#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import threading
import queue
import json

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
    from flask import Flask, render_template, request, send_file, send_from_directory, Response, jsonify
    from flask_cors import CORS
    import requests as http_requests
except ImportError:
    print("Instalando dependências...")
    install("flask")
    install("flask-cors")
    install("requests")
    from flask import Flask, render_template, request, send_file, send_from_directory, Response, jsonify
    from flask_cors import CORS
    import requests as http_requests

app = Flask(__name__)
CORS(app)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT', 5000))

# { "ESPN": {"embeds": [{"provider": "YouTube", "url": "..."}], "logo": "..."} }
channels = {}
_channels_lock = threading.Lock()
_sse_clients = []
_sse_lock = threading.Lock()


# ── SSE ───────────────────────────────────────────────────────────────────────

def channels_payload():
    with _channels_lock:
        data = dict(channels)
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

@app.route("/debug")
def debug():
    results = {}
    for path in ["/channels?category=Futebol", "/sports?category=Futebol&status=live"]:
        url = f"{URL_BASE}{path}"
        try:
            r = http_requests.get(url, headers=_HEADERS, timeout=10)
            results[path] = {"status": r.status_code, "ok": r.ok, "body": r.json()}
        except Exception as e:
            results[path] = {"error": type(e).__name__, "detail": str(e)}
    return jsonify({"url_base": URL_BASE, "results": results})


# ── API ──────────────────────────────────────────────────────────

URL_BASE = os.environ.get('URL_BASE', '')
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', 1800))

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": URL_BASE + "/",
    "Origin": URL_BASE + "/",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

def _get(path):
    url = f"{URL_BASE}{path}"
    r = http_requests.get(url, headers=_HEADERS, timeout=10)
    r.raise_for_status()
    return r

def _fetch_channels():
    """Returns (result_dict, ok) where ok=False means the request itself failed."""
    try:
        r = _get("/channels?category=Futebol")
        body = r.json()
        items = body if isinstance(body, list) else body.get("data", [])
        result = {}
        for ch in items:
            if not ch.get("is_active", True):
                continue
            name = ch.get("name") or ch.get("id", "")
            embed = ch.get("embed_url", "")
            if not name or not embed:
                continue
            result[name] = {
                "embeds": [{"provider": "stream", "url": embed}],
                "logo": ch.get("logo_url", ""),
            }
        log_sse(f"api: {len(result)} canal(is) de futebol encontrado(s)", "ok")
        return result, True
    except Exception as e:
        log_sse(f"api: erro /channels — {type(e).__name__}: {str(e)[:120]}", "err")
        return {}, False

def _fetch_live_events():
    """Returns (result_dict, ok) where ok=False means the request itself failed."""
    try:
        r = _get("/sports?category=Futebol&status=live")
        body = r.json()
        items = body if isinstance(body, list) else body.get("data", [])
        result = {}
        for ev in items:
            name = ev.get("title") or ev.get("id", "")
            embeds = [
                {"provider": e.get("provider", f"Fonte {i+1}"), "url": e.get("embed_url", "")}
                for i, e in enumerate(ev.get("embeds", []))
                if e.get("embed_url")
            ]
            if not name or not embeds:
                continue
            result[name] = {
                "embeds": embeds,
                "logo": ev.get("poster", ""),
            }
        log_sse(f"api: {len(result)} evento(s) ao vivo encontrado(s)", "ok")
        return result, True
    except Exception as e:
        log_sse(f"api: erro /sports — {type(e).__name__}: {str(e)[:120]}", "err")
        return {}, False


def fetch_all():
    if not URL_BASE:
        log_sse("api: URL_BASE não configurada no .env", "err")
        return
    log_sse("api: buscando canais e eventos ao vivo...", "inf")
    ch, ch_ok = _fetch_channels()
    ev, ev_ok = _fetch_live_events()
    merged = {**ch, **ev}

    if merged:
        with _channels_lock:
            channels.clear()
            channels.update(merged)
        notify_sse()
        log_sse(f"api: total {len(merged)} canal(is)/evento(s) carregado(s)", "inf")
    elif ch_ok and ev_ok:
        # API respondeu com sucesso mas não há jogos ao vivo — limpa a lista
        with _channels_lock:
            channels.clear()
        notify_sse()
        log_sse("api: nenhum jogo ao vivo no momento", "warn")
    else:
        # Ao menos uma requisição falhou — mantém dados anteriores para não sumir tudo
        log_sse("api: falha na requisição, mantendo dados anteriores", "warn")

def _fetch_loop():
    fetch_all()
    while True:
        time.sleep(POLL_INTERVAL)
        fetch_all()


# ── Startup ───────────────────────────────────────────────────────────────────

threading.Thread(target=_fetch_loop, daemon=True).start()

if __name__ == "__main__":
    print(f"Rodando na porta {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
