#!/usr/bin/env python3
import os
import re
import sys
import time
import subprocess
import threading
import queue
import json
import functools
from base64 import b64decode
from urllib.parse import urlparse

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


# ── SSE ───────────────────────────────────────────────────────────────────────

def channels_payload():
    with _channels_lock:
        data = {name: urls for name, urls in channels.items()}
    return json.dumps(data)

def _push_sse(msg):
    dead = []
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

def notify_sse():
    _push_sse(f"event: channels_updated\ndata: {channels_payload()}\n\n")

def log_sse(msg, t=""):
    payload = json.dumps({"msg": msg, "t": t})
    _push_sse(f"event: scrape_log\ndata: {payload}\n\n")

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
    show_log = 'showLog' in request.args
    return render_template("player.html", show_log=show_log)

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
    with _channels_lock:
        ch_snapshot = {name: list(urls) for name, urls in channels.items()}
    return render_template("admin.html", channels=ch_snapshot)

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


# ── Futemax scraper ───────────────────────────────────────────────────────────

FUTEMAX_BASE = os.environ.get('FUTEMAX_BASE', 'https://futemax.ad')
SCRAPE_INTERVAL = int(os.environ.get('SCRAPE_INTERVAL', 1800))

FUTEMAX_CHANNEL_MAP = {
    "PRIMEVIDEO": ["/prime-video-ao-vivo/"],
    "PREMIERE": [
        "/premiere-fc-ao-vivo-assista-online-em-hd/",
        "/premiere-2-ao-vivo-assista-online-em-hd-gratuitamente/",
        "/premiere-3-ao-vivo-assista-online-em-hd-gratuitamente/",
        "/premiere-4-ao-vivo-assista-online-em-hd-gratuitamente/",
        "/premiere-5-ao-vivo-assista-online-em-hd-gratuitamente/",
        "/premiere-6-ao-vivo-assista-online-em-hd-gratuitamente/",
        "/premiere-7-ao-vivo-assista-online-em-hd-gratuitamente/",
    ],
    "GLOBO": [
        "/globo-sp-ao-vivo-assista-online-em-hd/",
        "/globo-rj-ao-vivo-online-futebol-noticias-e-programas-em-hd/",
        "/globo-mg-ao-vivo/",
    ],
    "SPORTV": [
        "/sportv-ao-vivo-assista-esportes-online-em-hd/",
        "/sportv-2-ao-vivo-assista-esportes-em-hd/",
        "/sportv-3-ao-vivo-assista-esportes-em-hd/",
    ],
    "TNT": ["/tnt-ao-vivo-assista-futebol-internacional-em-hd-no-futemax/"],
    "SBT": ["/sbt-ao-vivo-assista-online-em-hd/"],
    "BAND": [
        "/band-tv-ao-vivo-assista-online-em-hd/",
        "/bandsports-ao-vivo-assista-esportes-em-hd/",
    ],
    "ESPN": [
        "/espn-ao-vivo-assista-esportes-online-em-hd/",
        "/espn-2-ao-vivo-assista-esportes-em-hd/",
        "/espn-3-ao-vivo-assista-esportes-em-hd/",
        "/espn-4-ao-vivo-assista-esportes-em-hd/",
    ],
}

_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

def _m3u8_from_player(player_url):
    try:
        r = http_requests.get(
            player_url,
            headers={**_SCRAPE_HEADERS, "Referer": FUTEMAX_BASE + "/"},
            timeout=10,
        )
        m = re.search(r'"stream"\s*:\s*"([^"]+\.m3u8[^"]*)"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

def _scrape_slug(slug):
    page_url = FUTEMAX_BASE + slug
    try:
        r = http_requests.get(page_url, headers=_SCRAPE_HEADERS, timeout=10)
        player_urls = re.findall(r'<button[^>]+data-src="([^"]+)"', r.text)
        if not player_urls:
            log_sse(f"scraper: sem botões em {slug}", "warn")
            return []
        found = []
        for pu in player_urls:
            m3u8 = _m3u8_from_player(pu)
            if m3u8:
                found.append(m3u8)
                log_sse(f"scraper: m3u8 encontrado via {urlparse(pu).netloc}", "ok")
            else:
                log_sse(f"scraper: sem m3u8 em {urlparse(pu).netloc}", "warn")
        return found
    except Exception as e:
        msg = str(e)
        short = msg[:80] + "..." if len(msg) > 80 else msg
        log_sse(f"scraper: erro em {slug} — {short}", "err")
        return []

def scrape_all_channels():
    log_sse("scraper: iniciando ciclo de busca...", "inf")
    updated = {}
    for channel, slugs in FUTEMAX_CHANNEL_MAP.items():
        urls = []
        for slug in slugs:
            urls.extend(_scrape_slug(slug))
        if urls:
            updated[channel] = urls
            log_sse(f"scraper: {channel} → {len(urls)} URL(s)", "ok")
        else:
            log_sse(f"scraper: {channel} → nenhuma URL encontrada", "warn")
    if updated:
        with _channels_lock:
            for ch, urls in updated.items():
                channels[ch] = urls
        notify_sse()
    log_sse(f"scraper: ciclo concluído — {len(updated)}/{len(FUTEMAX_CHANNEL_MAP)} canais atualizados", "inf")

def _scrape_loop():
    scrape_all_channels()
    while True:
        time.sleep(SCRAPE_INTERVAL)
        scrape_all_channels()


# ── Startup ───────────────────────────────────────────────────────────────────

threading.Thread(target=_scrape_loop, daemon=True).start()

if __name__ == "__main__":
    print(f"Rodando na porta {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
