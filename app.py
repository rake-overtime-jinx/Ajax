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
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ajax-" + str(uuid.uuid4())[:12])

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
XRAY_PORT = 10000
XRAY_API_PORT = 10085
FLASK_PORT = 5000
PUBLIC_PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = "panel.db"
XRAY_CONFIG = "xray.json"
NGINX_CONF = "nginx.conf"
ONLINE_THRESHOLD = 90
WS_PATH = "/cdn/assets"
ONLINE = {"last": 0}


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
        CREATE TABLE IF NOT EXISTS proxy (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            uuid TEXT NOT NULL,
            quota_gb REAL DEFAULT 100,
            used_bytes INTEGER DEFAULT 0,
            expire_days INTEGER DEFAULT 365,
            created_at TEXT,
            enabled INTEGER DEFAULT 1
        )
        """
    )
    c.execute("SELECT COUNT(*) FROM proxy")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO proxy (id, uuid, created_at) VALUES (1, ?, ?)",
            (str(uuid.uuid4()), datetime.now().isoformat()),
        )
    conn.commit()
    conn.close()


def get_proxy():
    conn = get_db()
    row = conn.execute("SELECT * FROM proxy WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def update_proxy(**kwargs):
    if not kwargs:
        return
    conn = get_db()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    conn.execute(f"UPDATE proxy SET {sets} WHERE id = 1", list(kwargs.values()))
    conn.commit()
    conn.close()


def is_active(p):
    if not p or p["enabled"] == 0:
        return False, "disabled"
    try:
        created = datetime.fromisoformat(p["created_at"])
        if (datetime.now() - created).days >= p["expire_days"]:
            return False, "expired"
    except Exception:
        pass
    used_gb = p["used_bytes"] / (1024 ** 3)
    if p["quota_gb"] > 0 and used_gb >= p["quota_gb"]:
        return False, "limit"
    return True, "active"


def enrich(p):
    if not p:
        return {}
    try:
        created = datetime.fromisoformat(p["created_at"])
        days_left = max(0, p["expire_days"] - (datetime.now() - created).days)
    except Exception:
        days_left = p["expire_days"]
    used_gb = round(p["used_bytes"] / (1024 ** 3), 2)
    quota = float(p["quota_gb"])
    percent = min(100, round((used_gb / quota) * 100, 1)) if quota > 0 else 0
    active, reason = is_active(p)
    p.update(
        {
            "used_gb": used_gb,
            "days_left": days_left,
            "percent": percent,
            "is_active": active,
            "reason": reason,
            "is_online": (time.time() - ONLINE.get("last", 0)) < ONLINE_THRESHOLD,
        }
    )
    return p


def xray_bin():
    for p in ("/usr/local/bin/xray/xray", "/usr/local/bin/xray", "xray"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return "xray"


def build_xray_config():
    p = get_proxy()
    clients = []
    if p and is_active(p)[0]:
        clients.append({"id": p["uuid"], "email": "edge", "level": 0})
    if not clients:
        clients.append({"id": str(uuid.uuid4()), "email": "disabled", "level": 0})
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
                "port": XRAY_API_PORT,
                "listen": "127.0.0.1",
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
            {
                "tag": "vless-in",
                "port": XRAY_PORT,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": WS_PATH, "acceptProxyProtocol": False},
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls"],
                },
            },
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {
            "rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]
        },
    }
    with open(XRAY_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    log(f"xray config written clients={len(clients)}")


def port_open(port, host="127.0.0.1"):
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def restart_xray():
    build_xray_config()
    subprocess.run(
        ["pkill", "-9", "-f", "xray"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    bin_path = xray_bin()
    try:
        proc = subprocess.Popen(
            [bin_path, "run", "-c", os.path.abspath(XRAY_CONFIG)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(0.8)
        if proc.poll() is not None:
            out = (proc.stdout.read() or b"").decode("utf-8", errors="ignore")[:500]
            log(f"xray exited early code={proc.returncode} out={out}")
        elif port_open(XRAY_PORT):
            log(f"xray running on {XRAY_PORT}")
        else:
            log("xray process alive but port not open yet")
    except Exception as e:
        log(f"xray start failed: {e}")


def write_nginx_conf():
    conf = f"""
worker_processes auto;
error_log /dev/stderr warn;
pid /tmp/nginx-ajax.pid;
events {{
    worker_connections 1024;
}}
http {{
    access_log /dev/stdout;
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
        location = {WS_PATH} {{
            proxy_pass http://127.0.0.1:{XRAY_PORT};
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
        location {WS_PATH}/ {{
            proxy_pass http://127.0.0.1:{XRAY_PORT};
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
            proxy_pass http://127.0.0.1:{FLASK_PORT};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}
    }}
}}
"""
    with open(NGINX_CONF, "w") as f:
        f.write(conf)


def restart_nginx():
    write_nginx_conf()
    subprocess.run(
        ["pkill", "-9", "-f", "nginx"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)
    conf_path = os.path.abspath(NGINX_CONF)
    try:
        test = subprocess.run(
            ["nginx", "-t", "-c", conf_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if test.returncode != 0:
            log(f"nginx config test failed: {test.stderr or test.stdout}")
            return
        subprocess.Popen(
            ["nginx", "-c", conf_path, "-g", "daemon off;"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(0.6)
        if port_open(PUBLIC_PORT, "127.0.0.1") or port_open(PUBLIC_PORT, "0.0.0.0"):
            log(f"nginx running on {PUBLIC_PORT}")
        else:
            log(f"nginx started but port {PUBLIC_PORT} not detected yet")
    except Exception as e:
        log(f"nginx start failed: {e}")


def query_stats():
    try:
        r = subprocess.run(
            [
                xray_bin(),
                "api",
                "statsquery",
                f"--server=127.0.0.1:{XRAY_API_PORT}",
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
            if not port_open(XRAY_PORT):
                log("xray port down, restarting")
                restart_xray()
            if not port_open(PUBLIC_PORT):
                log("public port down, restarting nginx")
                restart_nginx()
            bytes_added = query_stats()
            if bytes_added > 0:
                ONLINE["last"] = time.time()
                conn = get_db()
                conn.execute(
                    "UPDATE proxy SET used_bytes = used_bytes + ? WHERE id = 1",
                    (bytes_added,),
                )
                conn.commit()
                conn.close()
                p = get_proxy()
                if p and not is_active(p)[0]:
                    restart_xray()
        except Exception as e:
            log(f"stats loop error: {e}")


def make_configs(p, host):
    uid = p["uuid"]
    path_enc = urllib.parse.quote(WS_PATH, safe="")

    def remark(n):
        return urllib.parse.quote(f"Ajax Project | {n}")

    variants = [
        (
            "1",
            f"path={path_enc}&security=tls&alpn=http%2F1.1&fp=chrome&type=ws&sni={host}&allowInsecure=0",
            "443",
        ),
        (
            "2",
            f"path={path_enc}%3Fed%3D2560&security=tls&alpn=http%2F1.1&fp=chrome&type=ws&sni={host}&allowInsecure=0",
            "443",
        ),
        (
            "3",
            f"path={path_enc}&security=tls&alpn=h2%2Chttp%2F1.1&fp=firefox&type=ws&sni={host}&allowInsecure=0",
            "443",
        ),
        (
            "4",
            f"path={path_enc}&security=tls&alpn=http%2F1.1&fp=edge&type=ws&sni={host}&allowInsecure=0",
            "443",
        ),
        (
            "5",
            f"path={path_enc}%3Fed%3D2048&security=tls&alpn=h2&fp=chrome&type=ws&sni={host}&allowInsecure=0",
            "443",
        ),
        (
            "6",
            f"path={path_enc}&security=tls&fp=random&type=ws&sni={host}&allowInsecure=0",
            "443",
        ),
        ("7", f"path={path_enc}&security=none&type=ws", "80"),
    ]
    configs = []
    for num, qs, port in variants:
        link = f"vless://{uid}@{host}:{port}?{qs}&encryption=none&host={host}#{remark(num)}"
        configs.append({"title": f"Config {num}", "tag": f"A-{num}", "config": link})
    return configs


def sub_header(p):
    used = int(p["used_bytes"])
    total = int(float(p["quota_gb"]) * (1024 ** 3))
    try:
        exp = int(
            (
                datetime.fromisoformat(p["created_at"])
                + timedelta(days=p["expire_days"])
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
    xray_ok = port_open(XRAY_PORT)
    nginx_ok = port_open(PUBLIC_PORT)
    return jsonify(
        {
            "ok": True,
            "xray": xray_ok,
            "nginx": nginx_ok,
            "port": PUBLIC_PORT,
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
    p = enrich(get_proxy())
    return render_template("dashboard.html", p=p)


@app.route("/api/status")
def api_status():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    p = enrich(get_proxy())
    return jsonify(
        {
            "used_gb": p["used_gb"],
            "quota_gb": p["quota_gb"],
            "percent": p["percent"],
            "days_left": p["days_left"],
            "is_active": p["is_active"],
            "is_online": p["is_online"],
            "reason": p["reason"],
            "xray": port_open(XRAY_PORT),
            "nginx": port_open(PUBLIC_PORT),
        }
    )


@app.route("/api/update", methods=["POST"])
def api_update():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    data = request.get_json(silent=True) or {}
    try:
        quota = float(data.get("quota_gb", 0))
        days = int(data.get("expire_days", 0))
        if quota <= 0 or days <= 0:
            return jsonify({"error": "invalid values"}), 400
        update_proxy(quota_gb=quota, expire_days=days)
        restart_xray()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    update_proxy(used_bytes=0, created_at=datetime.now().isoformat())
    restart_xray()
    return jsonify({"ok": True})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    p = get_proxy()
    new_val = 0 if p["enabled"] == 1 else 1
    update_proxy(enabled=new_val)
    restart_xray()
    return jsonify({"ok": True, "enabled": new_val})


@app.route("/api/configs")
def api_configs():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    p = get_proxy()
    host = request.host.split(":")[0]
    configs = make_configs(p, host)
    sub = f"{request.host_url}sub/{p['uuid']}"
    return jsonify({"configs": configs, "sub": sub, "uuid": p["uuid"]})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    if "ok" not in session:
        return jsonify({"error": "auth"}), 401
    restart_xray()
    restart_nginx()
    return jsonify(
        {
            "ok": True,
            "xray": port_open(XRAY_PORT),
            "nginx": port_open(PUBLIC_PORT),
        }
    )


@app.route("/sub/<uid>")
def subscription(uid):
    p = get_proxy()
    if not p or p["uuid"] != uid:
        return "Not found", 404
    host = request.host.split(":")[0]
    active, reason = is_active(p)
    if active:
        configs = make_configs(p, host)
    else:
        fake = str(uuid.uuid4())
        remark = urllib.parse.quote(f"DISABLED ({reason})")
        path_enc = urllib.parse.quote(WS_PATH, safe="")
        configs = [
            {
                "config": f"vless://{fake}@{host}:443?type=ws&security=tls&path={path_enc}#{remark}"
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
    raw = "\n".join(c["config"] for c in configs)
    encoded = base64.b64encode(raw.encode()).decode()
    if is_client:
        resp = Response(encoded, mimetype="text/plain")
        resp.headers["Subscription-Userinfo"] = sub_header(p)
        return resp
    return render_template(
        "sub.html",
        used_gb=round(p["used_bytes"] / (1024 ** 3), 2),
        quota_gb=p["quota_gb"],
        days_left=enrich(p)["days_left"],
        percent=enrich(p)["percent"],
        configs=configs if active else [],
        sub_raw=encoded,
        sub_url=request.url,
        active=active,
        reason=reason,
    )


if __name__ == "__main__":
    log(f"boot public_port={PUBLIC_PORT}")
    init_db()
    restart_xray()
    restart_nginx()
    t = threading.Thread(target=stats_loop, daemon=True)
    t.start()
    log(f"flask on 127.0.0.1:{FLASK_PORT}")
    app.run(host="127.0.0.1", port=FLASK_PORT, threaded=True)
