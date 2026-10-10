
"""
Lightweight single-user edge service manager.
"""

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
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production-" + str(uuid.uuid4())[:8])

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")
XRAY_PORT = 10000
XRAY_API_PORT = 10085
FLASK_PORT = 5000
DB_PATH = "panel.db"
XRAY_CONFIG = "xray.json"
NGINX_CONF = "nginx.conf"
ONLINE_THRESHOLD = 90

WS_PATH = "/cdn/assets"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS proxy (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            uuid TEXT NOT NULL,
            quota_gb REAL DEFAULT 100,
            used_bytes INTEGER DEFAULT 0,
            expire_days INTEGER DEFAULT 365,
            created_at TEXT,
            enabled INTEGER DEFAULT 1
        )
    """)
    c.execute("SELECT COUNT(*) FROM proxy")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO proxy (id, uuid, created_at) VALUES (1, ?, ?)",
            (str(uuid.uuid4()), datetime.now().isoformat())
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
    p.update({
        "used_gb": used_gb,
        "days_left": days_left,
        "percent": percent,
        "is_active": active,
        "reason": reason,
        "is_online": (time.time() - ONLINE.get("last", 0)) < ONLINE_THRESHOLD
    })
    return p

ONLINE = {"last": 0}

def build_xray_config():
    p = get_proxy()
    clients = []
    if p and is_active(p)[0]:
        clients.append({"id": p["uuid"], "email": "edge", "level": 0})
    if not clients:
        clients.append({"id": str(uuid.uuid4()), "email": "disabled", "level": 0})

    cfg = {
        "log": {"loglevel": "none"},
        "stats": {},
        "api": {"tag": "api", "services": ["StatsService"]},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True}
        },
        "inbounds": [
            {
                "tag": "api",
                "port": XRAY_API_PORT,
                "listen": "127.0.0.1",
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"}
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
                    "wsSettings": {"path": WS_PATH}
                }
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {
            "rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]
        }
    }
    with open(XRAY_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)

def _xray_bin():
    for p in ("/usr/local/bin/xray/xray", "xray"):
        if os.path.exists(p) or p == "xray":
            return p
    return "xray"

def restart_xray():
    build_xray_config()
    subprocess.run(["pkill", "-9", "-f", "xray"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    try:
        
        subprocess.Popen(
            [_xray_bin(), "run", "-c", XRAY_CONFIG],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
    except Exception as e:
        print("core start error:", e)

def query_stats():
    try:
        r = subprocess.run(
            [_xray_bin(), "api", "statsquery",
             f"--server=127.0.0.1:{XRAY_API_PORT}",
             "-pattern=user>>>", "-reset=true"],
            capture_output=True, text=True, timeout=4
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
            bytes_added = query_stats()
            if bytes_added > 0:
                ONLINE["last"] = time.time()
                conn = get_db()
                conn.execute("UPDATE proxy SET used_bytes = used_bytes + ? WHERE id = 1",
                             (bytes_added,))
                conn.commit()
                conn.close()
                p = get_proxy()
                if p and not is_active(p)[0]:
                    restart_xray()
        except Exception:
            pass


def start_nginx():
    port = os.environ.get("PORT", "8080")
    
    conf = f"""
pid /tmp/nginx.pid;
error_log /dev/null;
events {{ worker_connections 512; }}
http {{
    access_log off;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
    server {{
        listen {port};
        server_name _;
        # Looks like a normal CDN / static asset endpoint
        location {WS_PATH} {{
            proxy_redirect off;
            proxy_pass http://127.0.0.1:{XRAY_PORT};
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Host $http_host;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
        }}
        location / {{
            proxy_pass http://127.0.0.1:{FLASK_PORT};
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }}
    }}
}}
"""
    with open(NGINX_CONF, "w") as f:
        f.write(conf)
    subprocess.run(["pkill", "-9", "-f", "nginx"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.2)
    subprocess.Popen(
        ["nginx", "-c", os.path.abspath(NGINX_CONF), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )


def make_configs(p, host):
    uid = p["uuid"]
    
    path_enc = urllib.parse.quote(WS_PATH, safe="")

    def remark(n):
        return urllib.parse.quote(f"Edge | {n}")

    variants = [
        ("1", f"path={path_enc}&security=tls&alpn=http%2F1.1&fp=chrome&type=ws&sni={host}&allowInsecure=0", "443"),
        ("2", f"path={path_enc}%3Fed%3D2560&security=tls&alpn=http%2F1.1&fp=chrome&type=ws&sni={host}&allowInsecure=0", "443"),
        ("3", f"path={path_enc}&security=tls&alpn=h2%2Chttp%2F1.1&fp=firefox&type=ws&sni={host}&allowInsecure=0", "443"),
        ("4", f"path={path_enc}&security=tls&alpn=http%2F1.1&fp=edge&type=ws&sni={host}&allowInsecure=0", "443"),
        ("5", f"path={path_enc}%3Fed%3D2048&security=tls&alpn=h2&fp=chrome&type=ws&sni={host}&allowInsecure=0", "443"),
        ("6", f"path={path_enc}&security=tls&fp=random&type=ws&sni={host}&allowInsecure=0", "443"),
        ("7", f"path={path_enc}&security=none&type=ws", "80"),
    ]
    configs = []
    for num, qs, port in variants:
        link = f"vless://{uid}@{host}:{port}?{qs}&encryption=none&host={host}#{remark(num)}"
        configs.append({
            "title": f"Config {num}",
            "tag": f"E-{num}",
            "config": link
        })
    return configs

def sub_header(p):
    used = int(p["used_bytes"])
    total = int(float(p["quota_gb"]) * (1024 ** 3))
    try:
        exp = int((datetime.fromisoformat(p["created_at"]) + timedelta(days=p["expire_days"])).timestamp())
    except Exception:
        exp = 0
    return f"upload=0; download={used}; total={total}; expire={exp}"


def require_auth():
    if "ok" not in session:
        return redirect(url_for("login"))
    return None

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
    return jsonify({
        "used_gb": p["used_gb"],
        "quota_gb": p["quota_gb"],
        "percent": p["percent"],
        "days_left": p["days_left"],
        "is_active": p["is_active"],
        "is_online": p["is_online"],
        "reason": p["reason"]
    })

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
        configs = [{"config": f"vless://{fake}@{host}:443?type=ws&security=tls&path={path_enc}#{remark}"}]

    ua = request.headers.get("User-Agent", "").lower()
    is_client = any(k in ua for k in ("v2ray", "clash", "sing-box", "hiddify", "nekobox", "shadowrocket", "v2box"))
    raw = "\n".join(c["config"] for c in configs)
    encoded = base64.b64encode(raw.encode()).decode()

    if is_client:
        resp = Response(encoded, mimetype="text/plain")
        resp.headers["Subscription-Userinfo"] = sub_header(p)
        return resp

    return render_template("sub.html",
                           used_gb=round(p["used_bytes"] / (1024 ** 3), 2),
                           quota_gb=p["quota_gb"],
                           days_left=enrich(p)["days_left"],
                           percent=enrich(p)["percent"],
                           configs=configs if active else [],
                           sub_raw=encoded,
                           sub_url=request.url,
                           active=active,
                           reason=reason)


if __name__ == "__main__":
    init_db()
    restart_xray()
    start_nginx()
    t = threading.Thread(target=stats_loop, daemon=True)
    t.start()
    
    app.run(host="127.0.0.1", port=FLASK_PORT, threaded=True)
