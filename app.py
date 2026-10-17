#!/usr/bin/env python3
import os
import sys
import uuid
import json
import base64
import sqlite3
import subprocess
import time
import threading
import urllib.parse
import urllib.request
import socket
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ajax-" + str(uuid.uuid4())[:12])

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
CORE_PORT = 10000
CORE_API_PORT = 10085
APP_PORT = 5000
PUBLIC_PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = "data.db"
CORE_CONFIG = "core.json"
GATE_CONF = "gate.conf"
ONLINE_THRESHOLD = 90
ASSET_PATH = "/cdn/assets"
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()
ONLINE = {"last": 0}
_IP_CACHE = {"ip": None, "ts": 0}


def log(msg):
    print(f"[ajax] {msg}", flush=True)


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token TEXT NOT NULL,
            limit_gb REAL DEFAULT 100,
            used_bytes INTEGER DEFAULT 0,
            expire_days INTEGER DEFAULT 365,
            created_at TEXT,
            enabled INTEGER DEFAULT 1
        )
        """
    )
    c.execute("SELECT COUNT(*) FROM account")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO account (id, token, created_at) VALUES (1, ?, ?)",
            (str(uuid.uuid4()), datetime.now().isoformat()),
        )
    conn.commit()
    conn.close()


def get_account():
    conn = get_db()
    row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def update_account(**kwargs):
    if not kwargs:
        return
    conn = get_db()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    conn.execute(f"UPDATE account SET {sets} WHERE id = 1", list(kwargs.values()))
    conn.commit()
    conn.close()


def is_active(a):
    if not a or a["enabled"] == 0:
        return False, "disabled"
    try:
        created = datetime.fromisoformat(a["created_at"])
        if (datetime.now() - created).days >= a["expire_days"]:
            return False, "expired"
    except Exception:
        pass
    used_gb = a["used_bytes"] / (1024 ** 3)
    if a["limit_gb"] > 0 and used_gb >= a["limit_gb"]:
        return False, "limit"
    return True, "active"


def enrich(a):
    if not a:
        return {}
    try:
        created = datetime.fromisoformat(a["created_at"])
        days_left = max(0, a["expire_days"] - (datetime.now() - created).days)
    except Exception:
        days_left = a["expire_days"]
    used_gb = round(a["used_bytes"] / (1024 ** 3), 2)
    limit = float(a["limit_gb"])
    percent = min(100, round((used_gb / limit) * 100, 1)) if limit > 0 else 0
    active, reason = is_active(a)
    a.update(
        {
            "used_gb": used_gb,
            "limit_gb": limit,
            "days_left": days_left,
            "percent": percent,
            "is_active": active,
            "reason": reason,
            "is_online": (time.time() - ONLINE.get("last", 0)) < ONLINE_THRESHOLD,
        }
    )
    return a


def core_bin():
    for p in ("/usr/local/bin/xray/xray", "/usr/local/bin/xray", "xray"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return "xray"


def resolve_host_ip(hostname):
    if not hostname:
        return None
    hostname = hostname.strip().lower()
    hostname = hostname.replace("https://", "").replace("http://", "").split("/")[0]
    hostname = hostname.split(":")[0]
    if not hostname or hostname.replace(".", "").isdigit():
        return None
    try:
        infos = socket.getaddrinfo(hostname, 443, socket.AF_INET)
        for item in infos:
            ip = item[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        return None
    return None


def service_hostnames(request_host=None):
    names = []
    if PUBLIC_HOST:
        names.append(PUBLIC_HOST)
    for key in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL", "RAILWAY_SERVICE_DOMAIN"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        raw = raw.replace("https://", "").replace("http://", "").split("/")[0]
        if raw:
            names.append(raw)
    if request_host:
        h = request_host.split(":")[0].strip().lower()
        if h.endswith("railway.app") or h.endswith("up.railway.app"):
            names.append(h)
    seen = set()
    out = []
    for n in names:
        n = n.strip().lower()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def pick_sni(request_host):
    hosts = service_hostnames(request_host)
    if hosts:
        return hosts[0]
    if request_host and not request_host.replace(".", "").isdigit():
        return request_host.split(":")[0]
    return request_host or "localhost"


def pick_address(request_host):
    now = time.time()
    if _IP_CACHE["ip"] and now - _IP_CACHE["ts"] < 120:
        return _IP_CACHE["ip"]
    for name in service_hostnames(request_host):
        ip = resolve_host_ip(name)
        if ip:
            _IP_CACHE["ip"] = ip
            _IP_CACHE["ts"] = now
            log("ingress address ready")
            return ip
    if request_host:
        h = request_host.split(":")[0]
        if h.endswith("railway.app"):
            ip = resolve_host_ip(h)
            if ip:
                _IP_CACHE["ip"] = ip
                _IP_CACHE["ts"] = now
                return ip
    return _IP_CACHE["ip"] or (request_host.split(":")[0] if request_host else "127.0.0.1")


def build_core_config():
    a = get_account()
    clients = []
    if a and is_active(a)[0]:
        clients.append({"id": a["token"], "email": "u1", "level": 0})
    if not clients:
        clients.append({"id": str(uuid.uuid4()), "email": "off", "level": 0})
    cfg = {
        "log": {"loglevel": "warning"},
        "stats": {},
        "api": {"tag": "api", "services": ["StatsService"]},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "inbounds": [
            {
                "tag": "api",
                "port": CORE_API_PORT,
                "listen": "127.0.0.1",
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
            {
                "tag": "in1",
                "port": CORE_PORT,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": ASSET_PATH},
                },
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {
            "rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]
        },
    }
    with open(CORE_CONFIG, "w") as f:
        json.dump(cfg, f)
    log("core config ready")


def port_open(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def restart_core():
    build_core_config()
    subprocess.run(
        ["pkill", "-9", "-f", "xray"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    bin_path = core_bin()
    try:
        proc = subprocess.Popen(
            [bin_path, "run", "-c", os.path.abspath(CORE_CONFIG)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(0.8)
        if proc.poll() is not None:
            log(f"core exit early {proc.returncode}")
        elif port_open(CORE_PORT):
            log("core ready")
        else:
            log("core starting")
    except Exception as e:
        log(f"core start error: {e}")


def write_gate_conf():
    conf = f"""
worker_processes auto;
error_log /dev/stderr warn;
pid /tmp/ajax-gate.pid;
events {{
    worker_connections 1024;
}}
http {{
    access_log off;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    keepalive_timeout 65;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}
    server {{
        listen {PUBLIC_PORT};
        listen [::]:{PUBLIC_PORT};
        server_name _;
        location = {ASSET_PATH} {{
            proxy_pass http://127.0.0.1:{CORE_PORT};
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
            proxy_buffering off;
        }}
        location {ASSET_PATH}/ {{
            proxy_pass http://127.0.0.1:{CORE_PORT};
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
            proxy_buffering off;
        }}
        location / {{
            proxy_pass http://127.0.0.1:{APP_PORT};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}
    }}
}}
"""
    with open(GATE_CONF, "w") as f:
        f.write(conf)


def restart_gate():
    write_gate_conf()
    subprocess.run(
        ["pkill", "-9", "-f", "nginx"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)
    conf_path = os.path.abspath(GATE_CONF)
    try:
        test = subprocess.run(
            ["nginx", "-t", "-c", conf_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if test.returncode != 0:
            log("gate config invalid")
            return
        subprocess.Popen(
            ["nginx", "-c", conf_path, "-g", "daemon off;"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(0.6)
        if port_open(PUBLIC_PORT):
            log("gate ready")
        else:
            log("gate starting")
    except Exception as e:
        log(f"gate error: {e}")


def query_stats():
    try:
        r = subprocess.run(
            [
                core_bin(),
                "api",
                "statsquery",
                f"--server=127.0.0.1:{CORE_API_PORT}",
                "-pattern=user>>>",
                "-reset=true",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if r.returncode != 0:
            return 0
        data = json.loads(r.stdout)
        total = 0
        for item in data.get("stat", []):
            total += int(item.get("value", 0))
        return total
    except Exception:
        return 0


def stats_loop():
    while True:
        time.sleep(20)
        try:
            if not port_open(CORE_PORT):
                log("core down, restart")
                restart_core()
            if not port_open(PUBLIC_PORT):
                log("gate down, restart")
                restart_gate()
            added = query_stats()
            if added > 0:
                ONLINE["last"] = time.time()
                conn = get_db()
                conn.execute(
                    "UPDATE account SET used_bytes = used_bytes + ? WHERE id = 1",
                    (added,),
                )
                conn.commit()
                conn.close()
                a = get_account()
                if a and not is_active(a)[0]:
                    restart_core()
        except Exception as e:
            log(f"loop error: {e}")


def make_links(a, request_host):
    token = a["token"]
    path_enc = urllib.parse.quote(ASSET_PATH, safe="")
    addr = pick_address(request_host)
    sni = pick_sni(request_host)
    host_header = sni

    def remark(n):
        return urllib.parse.quote(f"Ajax Project | {n}")

    variants = [
        (
            "1",
            f"path={path_enc}&security=tls&alpn=http%2F1.1&fp=chrome&type=ws&sni={sni}&host={host_header}&allowInsecure=1",
            "443",
        ),
        (
            "2",
            f"path={path_enc}&security=tls&alpn=http%2F1.1&fp=chrome&type=ws&sni={sni}&host={host_header}&allowInsecure=0",
            "443",
        ),
        (
            "3",
            f"path={path_enc}&security=tls&alpn=h2%2Chttp%2F1.1&fp=firefox&type=ws&sni={sni}&host={host_header}&allowInsecure=1",
            "443",
        ),
        (
            "4",
            f"path={path_enc}%3Fed%3D2560&security=tls&alpn=http%2F1.1&fp=chrome&type=ws&sni={sni}&host={host_header}&allowInsecure=1",
            "443",
        ),
        (
            "5",
            f"path={path_enc}&security=tls&fp=edge&type=ws&sni={sni}&host={host_header}&allowInsecure=1",
            "443",
        ),
        (
            "6",
            f"path={path_enc}&security=tls&fp=random&type=ws&sni={sni}&host={host_header}&allowInsecure=1",
            "443",
        ),
        (
            "7",
            f"path={path_enc}&security=none&type=ws&host={host_header}",
            "80",
        ),
    ]
    links = []
    for num, qs, port in variants:
        link = f"vless://{token}@{addr}:{port}?{qs}&encryption=none#{remark(num)}"
        links.append({"title": f"Link {num}", "tag": f"A-{num}", "config": link})
    return links, addr, sni


def usage_header(a):
    used = int(a["used_bytes"])
    total = int(float(a["limit_gb"]) * (1024 ** 3))
    try:
        exp = int(
            (
                datetime.fromisoformat(a["created_at"])
                + timedelta(days=a["expire_days"])
            ).timestamp()
        )
    except Exception:
        exp = 0
    return f"upload=0; download={used}; total={total}; expire={exp}"


def require_auth():
    if "ok" not in session:
        return redirect(url_for("login"))
    return None


@app.route("/ping")
def ping():
    return jsonify(
        {
            "ok": True,
            "core": port_open(CORE_PORT),
            "gate": port_open(PUBLIC_PORT),
            "port": PUBLIC_PORT,
            "addr": pick_address("") or "",
        }
    ), 200


@app.route("/")
def index():
    if "ok" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u == ADMIN_USER and p == ADMIN_PASS:
            session["ok"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    if (r := require_auth()):
        return r
    a = enrich(get_account())
    return render_template("dashboard.html", a=a)


@app.route("/api/status")
def api_status():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    a = enrich(get_account())
    return jsonify(
        {
            "used_gb": a["used_gb"],
            "limit_gb": a["limit_gb"],
            "percent": a["percent"],
            "days_left": a["days_left"],
            "is_active": a["is_active"],
            "is_online": a["is_online"],
            "reason": a["reason"],
            "core": port_open(CORE_PORT),
            "gate": port_open(PUBLIC_PORT),
            "addr": pick_address("") or "",
        }
    )


@app.route("/api/update", methods=["POST"])
def api_update():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    data = request.get_json(silent=True) or {}
    try:
        limit = float(data.get("limit_gb", 0))
        days = int(data.get("expire_days", 0))
        if limit <= 0 or days <= 0:
            return jsonify({"error": "invalid"}), 400
        update_account(limit_gb=limit, expire_days=days)
        restart_core()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    update_account(used_bytes=0, created_at=datetime.now().isoformat())
    restart_core()
    return jsonify({"ok": True})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    a = get_account()
    new_val = 0 if a["enabled"] == 1 else 1
    update_account(enabled=new_val)
    restart_core()
    return jsonify({"ok": True, "enabled": new_val})


@app.route("/api/links")
def api_links():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    a = get_account()
    host = request.host.split(":")[0]
    links, addr, sni = make_links(a, host)
    feed = f"{request.host_url}feed/{a['token']}"
    return jsonify(
        {
            "links": links,
            "feed": feed,
            "token": a["token"],
            "addr": addr,
            "sni": sni,
        }
    )


@app.route("/api/restart", methods=["POST"])
def api_restart():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    restart_core()
    restart_gate()
    _IP_CACHE["ip"] = None
    return jsonify(
        {
            "ok": True,
            "core": port_open(CORE_PORT),
            "gate": port_open(PUBLIC_PORT),
            "addr": pick_address("") or "",
        }
    )


@app.route("/feed/<token>")
def feed(token):
    a = get_account()
    if not a or a["token"] != token:
        return "Not found", 404
    host = request.host.split(":")[0]
    active, reason = is_active(a)
    if active:
        links, _, _ = make_links(a, host)
    else:
        fake = str(uuid.uuid4())
        remark = urllib.parse.quote(f"OFF ({reason})")
        path_enc = urllib.parse.quote(ASSET_PATH, safe="")
        addr = pick_address(host)
        links = [
            {
                "config": f"vless://{fake}@{addr}:443?type=ws&security=tls&path={path_enc}#{remark}"
            }
        ]
    ua = request.headers.get("User-Agent", "").lower()
    is_client = any(
        k in ua
        for k in (
            "v2ray",
            "clash",
            "sing-box",
            "hiddify",
            "nekobox",
            "shadowrocket",
            "v2box",
        )
    )
    raw = "\n".join(c["config"] for c in links)
    encoded = base64.b64encode(raw.encode()).decode()
    if is_client:
        resp = Response(encoded, mimetype="text/plain")
        resp.headers["Subscription-Userinfo"] = usage_header(a)
        return resp
    return render_template(
        "feed.html",
        used_gb=round(a["used_bytes"] / (1024 ** 3), 2),
        limit_gb=a["limit_gb"],
        days_left=enrich(a)["days_left"],
        percent=enrich(a)["percent"],
        links=links if active else [],
        active=active,
        reason=reason,
    )


if __name__ == "__main__":
    log(f"boot port={PUBLIC_PORT}")
    init_db()
    restart_core()
    restart_gate()
    pick_address(None)
    threading.Thread(target=stats_loop, daemon=True).start()
    log(f"app on {APP_PORT}")
    app.run(host="127.0.0.1", port=APP_PORT, threaded=True)
