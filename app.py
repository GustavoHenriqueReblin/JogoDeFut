#!/usr/bin/env python3
import os, json, time, threading, queue, urllib.parse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, send_file
from flask_cors import CORS
import requests as http_req

from scraper import (
    scrape_listings, resolve_stream,
    debug_screenshot, debug_resolve, debug_scrape,
    debug_live_frames, debug_live_resolve_frames,
)

app = Flask(__name__)
CORS(app)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
PORT          = int(os.environ.get("PORT", 5000))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 1800))
DEBUG_KEY     = os.environ.get("DEBUG_KEY", "")

listings  = {}
_lock     = threading.Lock()
_clients  = []
_cli_lock = threading.Lock()


# ── SSE ──────────────────────────────────────────────────────────────────────

def _push(msg):
    with _cli_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _clients.remove(q)

def _notify():
    with _lock:
        data = dict(listings)
    _push(f"event: listings_updated\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

def _log(msg, t=""):
    _push(f"event: log\ndata: {json.dumps({'msg': msg, 't': t})}\n\n")


@app.route("/events")
def sse():
    def stream():
        q = queue.Queue()
        with _cli_lock:
            _clients.append(q)
        try:
            with _lock:
                data = dict(listings)
            yield f"event: listings_updated\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _cli_lock:
                if q in _clients:
                    _clients.remove(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Resolve ───────────────────────────────────────────────────────────────────

@app.route("/resolve")
def resolve():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url obrigatória"}), 400
    try:
        return jsonify(resolve_stream(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── HLS Proxy ─────────────────────────────────────────────────────────────────

_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


@app.route("/proxy/m3u8")
def proxy_m3u8():
    url     = request.args.get("url", "")
    referer = request.args.get("ref", "")
    if not url:
        return "url obrigatória", 400

    headers = dict(_PROXY_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Origin"]  = referer.rstrip("/").rsplit("/", 1)[0]

    try:
        r = http_req.get(url, headers=headers, timeout=10)
        r.raise_for_status()

        lines = []
        for line in r.text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                seg = stripped if stripped.startswith("http") else urllib.parse.urljoin(url, stripped)
                line = "/proxy/ts?url=" + urllib.parse.quote(seg, safe="")
            lines.append(line)

        return Response(
            "\n".join(lines),
            mimetype="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )
    except Exception as e:
        return str(e), 502


@app.route("/proxy/ts")
def proxy_ts():
    url = urllib.parse.unquote(request.args.get("url", ""))
    if not url:
        return "url obrigatória", 400
    try:
        r = http_req.get(url, headers=_PROXY_HEADERS, timeout=20, stream=True)
        r.raise_for_status()

        def generate():
            for chunk in r.iter_content(chunk_size=65536):
                yield chunk

        return Response(
            generate(),
            mimetype="video/mp2t",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=30"},
        )
    except Exception as e:
        return str(e), 502


# ── Debug ─────────────────────────────────────────────────────────────────────

def _check_debug_key():
    if not DEBUG_KEY:
        return "DEBUG_KEY não configurado no .env", 403
    if request.args.get("key") != DEBUG_KEY:
        return "chave inválida", 403
    return None


@app.route("/debug/screenshot")
def debug_route_screenshot():
    err = _check_debug_key()
    if err:
        return err

    url = request.args.get("url", "").strip()
    if not url:
        return "parâmetro url obrigatório", 400

    try:
        png = debug_screenshot(url)
        return Response(png, mimetype="image/png")
    except Exception as e:
        return str(e), 500


@app.route("/debug/resolve")
def debug_route_resolve():
    err = _check_debug_key()
    if err:
        return err

    url = request.args.get("url", "").strip()
    if not url:
        return "parâmetro url obrigatório", 400

    try:
        return jsonify(debug_resolve(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug/live")
def debug_route_live():
    err = _check_debug_key()
    if err:
        return err

    url = request.args.get("url", "").strip()
    if not url:
        # HTML helper page
        html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <title>Debug — Live View</title>
  <style>
    body{font-family:monospace;background:#0a0a0f;color:#e8e8f0;padding:24px;margin:0}
    h2{color:#e8ff47}
    input{width:60%;padding:8px;background:#111118;border:1px solid #333;color:#e8e8f0;border-radius:4px}
    button{padding:8px 16px;background:#e8ff47;color:#0a0a0f;border:none;border-radius:4px;cursor:pointer;margin-left:8px}
    label{display:block;margin:12px 0 4px}
    #frame{margin-top:20px;max-width:100%;border:1px solid #1e1e2e;border-radius:8px}
  </style>
</head>
<body>
  <h2>Live View — Playwright</h2>
  <label>URL da página</label>
  <input id="url" placeholder="https://..."/>
  <button onclick="watch(false)">Assistir</button>
  <button onclick="watch(true)">Assistir + Resolve</button>
  <img id="frame" src="" alt="aguardando..."/>
  <script>
    function watch(resolve) {
      const url = document.getElementById('url').value.trim();
      if (!url) return;
      const key = new URLSearchParams(location.search).get('key') || '';
      const src = '/debug/live?url=' + encodeURIComponent(url)
                + '&key=' + encodeURIComponent(key)
                + (resolve ? '&resolve=1' : '');
      document.getElementById('frame').src = src;
    }
  </script>
</body>
</html>"""
        return Response(html, mimetype="text/html")

    resolve_mode = request.args.get("resolve") == "1"
    gen = debug_live_resolve_frames(url) if resolve_mode else debug_live_frames(url)

    def mjpeg():
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        try:
            for frame in gen:
                yield boundary + frame + b"\r\n"
        except Exception:
            pass

    return Response(
        mjpeg(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"},
    )


@app.route("/debug/scrape")
def debug_route_scrape():
    err = _check_debug_key()
    if err:
        return err

    try:
        data = debug_scrape()
        # Retorna página HTML com o screenshot embutido + JSON dos dados
        screenshot = data.pop("screenshot_base64", None)
        html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <title>Debug — Scrape</title>
  <style>
    body{{font-family:monospace;background:#0a0a0f;color:#e8e8f0;padding:24px;margin:0}}
    h2{{color:#e8ff47;margin-bottom:12px}}
    img{{max-width:100%;border:1px solid #1e1e2e;border-radius:8px;margin-bottom:24px}}
    pre{{background:#111118;border:1px solid #1e1e2e;border-radius:8px;padding:16px;
         overflow:auto;font-size:.8rem;white-space:pre-wrap;word-break:break-all}}
  </style>
</head>
<body>
  <h2>Screenshot</h2>
  {"<img src='data:image/png;base64," + screenshot + "'/>" if screenshot else "<p>sem screenshot</p>"}
  <h2>Dados encontrados</h2>
  <pre>{json.dumps(data, ensure_ascii=False, indent=2)}</pre>
</body>
</html>"""
        return Response(html, mimetype="text/html")
    except Exception as e:
        return str(e), 500


# ── Static ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("player.html", show_log="showLog" in request.args)

@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR, "manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def sw_js():
    return send_from_directory(BASE_DIR, "sw.js", mimetype="application/javascript")

@app.route("/favicon.ico")
def favicon():
    return send_file(os.path.join(BASE_DIR, "static", "icons", "logo-48.png"), mimetype="image/png")


# ── Background scraper ────────────────────────────────────────────────────────

def _scrape_loop():
    while True:
        _log("buscando listagem...", "inf")
        try:
            data = scrape_listings()
            with _lock:
                listings.clear()
                listings.update(data)
            _notify()
            games    = sum(1 for v in data.values() if v["type"] == "game")
            channels = sum(1 for v in data.values() if v["type"] == "channel")
            _log(f"{games} jogo(s) ao vivo · {channels} canal(is)", "ok")
        except Exception as e:
            _log(f"erro ao buscar listagem: {e}", "err")
        time.sleep(POLL_INTERVAL)


threading.Thread(target=_scrape_loop, daemon=True).start()

if __name__ == "__main__":
    print(f"Rodando na porta {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
