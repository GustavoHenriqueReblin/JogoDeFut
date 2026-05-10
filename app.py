#!/usr/bin/env python3
import os, urllib.parse, base64, secrets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, send_file
from flask_cors import CORS
import requests as http_req

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from scraper import resolve_stream

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


# ── Stream (resolve + proxy m3u8 em um só passo) ──────────────────────────────

_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


@app.route("/stream")
def stream():
    raw = request.args.get("url", "").strip()
    if not raw:
        return "url obrigatória", 400
    try:
        channel_url = _decrypt_url(raw)
    except Exception:
        return "url inválida", 400

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
