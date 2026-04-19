#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import queue
import json
import functools
from base64 import b64decode

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

def install(pkg):
    subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    from flask import Flask, render_template, request, send_file, send_from_directory, jsonify, redirect, url_for, Response
    from flask_cors import CORS
    import requests as http_requests
except ImportError:
    print("Instalando dependências...")
    install("flask")
    install("flask-cors")
    install("requests")
    from flask import Flask, render_template, request, send_file, send_from_directory, jsonify, redirect, url_for, Response
    from flask_cors import CORS
    import requests as http_requests

app = Flask(__name__)
CORS(app)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT', 5000))
DEFAULT_URL = os.environ.get('START_URL', '')
current_url = DEFAULT_URL
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'admin')

def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Basic '):
            try:
                user, pw = b64decode(auth[6:]).decode().split(':', 1)
                if user == ADMIN_USER and pw == ADMIN_PASS:
                    return f(*args, **kwargs)
            except Exception:
                pass
        return Response('Acesso negado', 401,
                        {'WWW-Authenticate': 'Basic realm="Admin"'})
    return wrapper

# { "GLOBO": ["url1", "url2", ...] }
channels = {}
_channels_lock = threading.Lock()
_sse_clients = []
_sse_lock = threading.Lock()


# ── Channel data ──────────────────────────────────────────────────────────────

def parse_domains(text):
    result = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith(':') and not line.startswith('http'):
            current = line[:-1].strip().upper()
            result[current] = []
        elif current and line.startswith('http'):
            result[current].append(line)
    return result

def load_channels():
    global channels
    data = os.environ.get('CHANNELS_DATA', '').replace('\\n', '\n')
    if not data:
        path = os.path.join(BASE_DIR, 'domains.txt')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                data = f.read()
    if data:
        with _channels_lock:
            channels = parse_domains(data)


# ── SSE ───────────────────────────────────────────────────────────────────────

def channels_payload():
    with _channels_lock:
        data = {name: urls for name, urls in channels.items()}
    return json.dumps(data)

def notify_sse():
    msg = f"event: channels_updated\ndata: {channels_payload()}\n\n"
    dead = []
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

@app.route("/events")
def sse():
    def stream():
        q = queue.Queue()
        with _sse_lock:
            _sse_clients.append(q)
        try:
            yield f"event: channels_updated\ndata: {channels_payload()}\n\n"
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Public API ────────────────────────────────────────────────────────────────

@app.route("/check")
def check_url():
    url = request.args.get("url", "").strip()
    if not url.startswith("http"):
        return jsonify({"ok": False})
    try:
        r = http_requests.get(url, timeout=8)
        if r.status_code != 200:
            return jsonify({"ok": False})
        base = url.rsplit("/", 1)[0]
        segment = None
        for line in r.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            segment = line if line.startswith("http") else base + "/" + line
            break
        if not segment:
            return jsonify({"ok": True})
        seg_r = http_requests.get(segment, timeout=6, stream=True)
        return jsonify({"ok": seg_r.status_code == 200})
    except Exception:
        return jsonify({"ok": False})


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    url = request.args.get('url', current_url)
    show_log = 'showLog' in request.args
    return render_template("player.html", url=url, show_log=show_log)

@app.route("/current_url")
def get_current_url():
    return current_url

@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json', mimetype='application/manifest+json')

@app.route("/sw.js")
def sw():
    return send_from_directory(BASE_DIR, 'sw.js', mimetype='application/javascript')

@app.route("/favicon.ico")
def favicon():
    return send_file(os.path.join(BASE_DIR, "static", "icons", "logo-48.png"), mimetype="image/png")


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
@require_auth
def admin():
    global current_url
    message = ""
    if request.method == "POST":
        new_url = request.form.get("url", "").strip()
        current_url = new_url
        message = f"URL atualizada: {new_url}"
    with _channels_lock:
        ch_snapshot = {name: list(urls) for name, urls in channels.items()}
    return render_template("admin.html", current_url=current_url, message=message, channels=ch_snapshot)

@app.route("/admin/channels", methods=["POST"])
@require_auth
def admin_create_channel():
    name = request.form.get("name", "").strip().upper()
    if not name:
        return "Nome inválido", 400
    with _channels_lock:
        if name not in channels:
            channels[name] = []
    notify_sse()
    return redirect(url_for("admin"))

@app.route("/admin/channels/<name>/delete", methods=["POST"])
@require_auth
def admin_delete_channel(name):
    with _channels_lock:
        channels.pop(name, None)
    notify_sse()
    return redirect(url_for("admin"))

@app.route("/admin/channels/<name>/urls", methods=["POST"])
@require_auth
def admin_add_url(name):
    url = request.form.get("url", "").strip()
    if not url:
        return "URL inválida", 400
    with _channels_lock:
        if name in channels:
            channels[name].append(url)
    notify_sse()
    return redirect(url_for("admin"))

@app.route("/admin/channels/<name>/urls/<int:idx>/delete", methods=["POST"])
@require_auth
def admin_remove_url(name, idx):
    with _channels_lock:
        urls = channels.get(name, [])
        if 0 <= idx < len(urls):
            urls.pop(idx)
    notify_sse()
    return redirect(url_for("admin"))


# ── Startup ───────────────────────────────────────────────────────────────────

load_channels()

if __name__ == "__main__":
    print(f"Rodando na porta {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
