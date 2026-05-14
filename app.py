#!/usr/bin/env python3
import os, urllib.parse, base64, secrets, time, threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, send_file
from flask_cors import CORS
import requests as http_req

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from scraper import resolve_stream, _CACHE, _CACHE_TTL

import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PORT         = int(os.environ.get("PORT", 5000))
PROXY_SECRET = os.environ.get("PROXY_SECRET") or secrets.token_hex(16)

def _decrypt_url(enc: str) -> str:
    data = base64.urlsafe_b64decode(enc + "==")
    iv, ct = data[:12], data[12:]
    return AESGCM(bytes.fromhex(PROXY_SECRET)).decrypt(iv, ct, None).decode()

def _encrypt_url(url: str) -> str:
    iv  = secrets.token_bytes(12)
    ct  = AESGCM(bytes.fromhex(PROXY_SECRET)).encrypt(iv, url.encode(), None)
    return base64.urlsafe_b64encode(iv + ct).rstrip(b"=").decode()


def _parse_channels():
    channels = []
    for entry in os.environ.get("CHANNELS", "").split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        name, url = entry.split(":", 1)
        channels.append({"name": name.strip(), "url": url.strip()})
    return channels


@app.route("/channels")
def channels():
    return jsonify([
        {"name": ch["name"], "url": _encrypt_url(ch["url"])}
        for ch in _parse_channels()
    ])


def _match_channel(provider: str, channel_list: list) -> dict | None:
    """Retorna o canal cujo nome está contido no provider da API (case-insensitive)."""
    p = provider.lower()
    for ch in channel_list:
        if ch["name"].lower() in p:
            return ch
    return None


_MOCK_GAMES = os.environ.get("MOCK_GAMES", "").lower() == "true"

_MOCK_API_DATA = {
    "data": [
        {
            "id": "avai-x-fortaleza",
            "title": "Avaí x Fortaleza",
            "description": "Brasileirão Série B",
            "poster": "https://i.imgur.com/5O8YIZv.jpeg",
            "start_time": "2026-05-10 18:30:00",
            "end_time": "2026-05-10 20:30:00",
            "embeds": [
                {"provider": "Disney+",             "embed_url": "https://esportesembed.com/avai-x-fortaleza-1"},
                {"provider": "Disney+ (Alternativo)","embed_url": "https://esportesembed.com/avai-x-fortaleza-2"},
            ],
        },
        {
            "id": "corinthians-x-sao-paulo",
            "title": "Corinthians x São Paulo",
            "description": "Brasileirão",
            "poster": "https://i.imgur.com/99GwG0p.png",
            "start_time": "2026-05-10 18:30:00",
            "end_time": "2026-05-10 20:30:00",
            "embeds": [
                {"provider": "Prime Video",               "embed_url": "https://esportesembed.com/corinthians-x-sao-paulo-1"},
                {"provider": "Prime Video (Alternativo)",  "embed_url": "https://esportesembed.com/corinthians-x-sao-paulo-2"},
                {"provider": "Prime Video (Alternativo 2)","embed_url": "https://esportesembed.com/corinthians-x-sao-paulo-3"},
            ],
        },
        {
            "id": "santos-x-red-bull-bragantino",
            "title": "Santos x Red Bull Bragantino",
            "description": "Brasileirão",
            "poster": "https://i.imgur.com/Yb2wTjQ.png",
            "start_time": "2026-05-10 18:30:00",
            "end_time": "2026-05-10 20:30:00",
            "embeds": [
                {"provider": "Premiere 3",            "embed_url": "https://esportesembed.com/santos-x-red-bull-bragantino-1"},
                {"provider": "Premiere 3",            "embed_url": "https://esportesembed.com/santos-x-red-bull-bragantino-2"},
                {"provider": "Premiere 3 (Alternativo)","embed_url": "https://esportesembed.com/santos-x-red-bull-bragantino-3"},
            ],
        },
        {
            "id": "gremio-x-flamengo",
            "title": "Grêmio x Flamengo",
            "description": "Brasileirão",
            "poster": "https://i.imgur.com/wB5pjJ6.png",
            "start_time": "2026-05-10 19:30:00",
            "end_time": "2026-05-10 21:30:00",
            "embeds": [
                {"provider": "Premiere CLubes",               "embed_url": "https://esportesembed.com/gremio-x-flamengo-1"},
                {"provider": "Premiere Clubes (Alternativo)",  "embed_url": "https://esportesembed.com/gremio-x-flamengo-2"},
                {"provider": "Premiere Clubes (Alternativo 2)","embed_url": "https://esportesembed.com/gremio-x-flamengo-3"},
            ],
        },
        {
            "id": "novorizontino-x-botafogo-sp",
            "title": "Novorizontino x Botafogo-SP",
            "description": "Brasileirão Série B",
            "poster": "https://i.imgur.com/wxz2TGq.jpeg",
            "start_time": "2026-05-10 19:30:00",
            "end_time": "2026-05-10 21:30:00",
            "embeds": [
                {"provider": "ESPN",    "embed_url": "https://esportesembed.com/novorizontino-x-botafogo-sp-1"},
                {"provider": "Disney+", "embed_url": "https://esportesembed.com/novorizontino-x-botafogo-sp-2"},
            ],
        },
    ]
}

@app.route("/games")
def games():
    if _MOCK_GAMES:
        data = _MOCK_API_DATA
    else:
        try:
            r = http_req.get(
                "https://api.reidoscanais.ooo/sports?categories=Futebol&status=live",
                timeout=8,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[games] erro ao buscar API: {e}")
            return jsonify([])

    channel_list = _parse_channels()
    result = []

    for game in data.get("data", []):
        matched_embeds = []
        for embed in game.get("embeds", []):
            ch = _match_channel(embed.get("provider", ""), channel_list)
            if ch:
                matched_embeds.append({
                    "provider": embed["provider"],
                    "channel_name": ch["name"],
                    "channel_url": _encrypt_url(ch["url"]),
                })

        if not matched_embeds:
            continue

        result.append({
            "id":          game["id"],
            "title":       game["title"],
            "description": game.get("description", ""),
            "poster":      game.get("poster", ""),
            "start_time":  game.get("start_time", "")[:16],
            "end_time":    game.get("end_time", "")[:16],
            "embeds":      matched_embeds,
        })

    return jsonify(result)


# ── Resolve assíncrono ───────────────────────────────────────────────────────

_RESOLVE_STATUS: dict[str, str] = {}  # encrypted_url -> "loading" | "ready" | "error"
_RESOLVE_LOCK = threading.Lock()


@app.route("/resolve")
def resolve_start():
    raw = request.args.get("url", "").strip()
    if not raw:
        return jsonify({"status": "error"}), 400
    try:
        channel_url = _decrypt_url(raw)
    except Exception:
        return jsonify({"status": "error"}), 400

    cached = _CACHE.get(channel_url)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        _RESOLVE_STATUS[raw] = "ready"
        return jsonify({"status": "ready"})

    with _RESOLVE_LOCK:
        if _RESOLVE_STATUS.get(raw) == "loading":
            return jsonify({"status": "loading"})
        _RESOLVE_STATUS[raw] = "loading"

    def _bg():
        try:
            result = resolve_stream(channel_url)
            _RESOLVE_STATUS[raw] = "ready" if result.get("streams") else "error"
        except Exception:
            _RESOLVE_STATUS[raw] = "error"

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "loading"})


@app.route("/resolve/status")
def resolve_status():
    raw = request.args.get("url", "").strip()
    status = _RESOLVE_STATUS.get(raw, "unknown")
    return jsonify({"status": status})


# ── Stream (resolve + proxy m3u8 em um só passo) ──────────────────────────────

_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


@app.route("/stream")
def stream():
    raw = request.args.get("url", "").strip()
    if not raw:
        return "url obrigatória", 400
    try:
        channel_url = _decrypt_url(raw)
    except Exception:
        return "url inválida", 400

    ip = _client_ip()
    print(f"[stream] ip={ip} canal={channel_url}")

    try:
        result = resolve_stream(channel_url)
    except Exception as e:
        return str(e), 502

    if not result.get("streams"):
        return "stream não encontrado", 404

    m3u8_url = result["streams"][0]["url"]
    referer  = result["streams"][0].get("referer", "")

    headers = dict(_PROXY_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Origin"]  = referer.rstrip("/").rsplit("/", 1)[0]

    try:
        r = http_req.get(m3u8_url, headers=headers, timeout=10)
        r.raise_for_status()

        lines = []
        for line in r.text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                seg = stripped if stripped.startswith("http") else urllib.parse.urljoin(m3u8_url, stripped)
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

@app.route("/debug/screenshot")
def debug_screenshot():
    path = "/tmp/camoufox_debug.png"
    if not os.path.exists(path):
        return "nenhum screenshot disponível", 404
    return send_file(path, mimetype="image/png")


@app.route("/debug")
def debug_page():
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Debug</title>
<style>body{background:#111;color:#eee;font-family:monospace;padding:16px}
img{max-width:100%;border:1px solid #333;display:block;margin-top:8px}
#status{font-size:.8rem;color:#888;margin-top:6px}</style>
</head><body>
<h3>camoufox screenshot</h3>
<div id="status">aguardando...</div>
<img id="shot" src="" alt="screenshot">
<script>
let lastMod = null;
async function refresh() {
  try {
    const r = await fetch('/debug/screenshot/meta');
    const d = await r.json();
    if (d.mtime !== lastMod) {
      lastMod = d.mtime;
      document.getElementById('shot').src = '/debug/screenshot?t=' + Date.now();
      document.getElementById('status').textContent = 'atualizado: ' + new Date(d.mtime * 1000).toLocaleTimeString();
    }
  } catch {}
}
refresh();
setInterval(refresh, 2000);
</script>
</body></html>"""


@app.route("/debug/screenshot/meta")
def debug_screenshot_meta():
    path = "/tmp/camoufox_debug.png"
    if not os.path.exists(path):
        return jsonify({"mtime": None})
    return jsonify({"mtime": os.path.getmtime(path)})


# ── Static ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("player.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR, "manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def sw_js():
    return send_from_directory(BASE_DIR, "sw.js", mimetype="application/javascript")

@app.route("/favicon.ico")
def favicon():
    return send_file(os.path.join(BASE_DIR, "static", "icons", "logo-48.png"), mimetype="image/png")


if __name__ == "__main__":
    print(f"Rodando na porta {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
