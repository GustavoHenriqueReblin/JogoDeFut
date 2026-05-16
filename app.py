#!/usr/bin/env python3
import os, urllib.parse, base64, secrets, time, threading, queue

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, send_file
from flask_cors import CORS
import requests as http_req

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from scraper import resolve_stream, _CACHE, _CACHE_TTL, _log

import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

app = Flask(__name__)
CORS(app)

_ACTIVE_IPS: dict[str, float] = {}
_ACTIVE_LOCK = threading.Lock()
_IP_TTL = 30
_STATUS_SUBSCRIBERS: list[queue.Queue] = []
_STATUS_LOCK = threading.Lock()

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


def _normalize(s: str) -> str:
    return s.lower().replace(" ", "")

def _match_channel(provider: str, channel_list: list) -> dict | None:
    p = _normalize(provider)
    for ch in channel_list:
        if _normalize(ch["name"]) == p:
            return ch
    return None


_GAMES_API_URL = os.environ["GAMES_API_URL"]

@app.route("/games")
def games():
    try:
        r = http_req.get(_GAMES_API_URL, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        app.logger.error("[games] erro ao buscar API: %s", e)
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
    with _ACTIVE_LOCK:
        is_new = ip not in _ACTIVE_IPS or (time.time() - _ACTIVE_IPS[ip]) >= _IP_TTL
        _ACTIVE_IPS[ip] = time.time()
        if is_new:
            active = sum(1 for t in _ACTIVE_IPS.values() if time.time() - t < _IP_TTL)
            print(f"Novo IP ({ip}) conectado. Total ativos: ({active}).")
            _push_status(active)

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


def _push_status(count: int):
    with _STATUS_LOCK:
        for q in _STATUS_SUBSCRIBERS:
            q.put_nowait(count)



@app.route("/status")
def status():
    now = time.time()
    with _ACTIVE_LOCK:
        active = [ip for ip, t in _ACTIVE_IPS.items() if now - t < _IP_TTL]
    return jsonify({"devices": len(active)})


@app.route("/status/stream")
def status_stream():
    q = queue.Queue()
    with _STATUS_LOCK:
        _STATUS_SUBSCRIBERS.append(q)

    def generate():
        now = time.time()
        with _ACTIVE_LOCK:
            count = sum(1 for t in _ACTIVE_IPS.values() if now - t < _IP_TTL)
        yield f"data: {count}\n\n"
        try:
            while True:
                try:
                    count = q.get(timeout=30)
                    yield f"data: {count}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with _STATUS_LOCK:
                _STATUS_SUBSCRIBERS.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


_WARMUP_COOLDOWN = 300        # pausa de 5 min se parecer bloqueio de IP
_WARMUP_CONSECUTIVE_FAIL = 3  # quantas falhas seguidas disparam o cooldown


def _warmup_pass(channels: list, label: str) -> list:
    """Resolve cada canal da lista. Retorna os que falharam."""
    import random
    failed = []
    consecutive_fails = 0
    random.shuffle(channels)
    _log(f"[warmup] {label}: {len(channels)} canais...")
    for i, ch in enumerate(channels, 1):
        delay = random.uniform(7, 17)
        _log(f"[warmup] {label} [{i}/{len(channels)}] aguardando {delay:.1f}s antes de resolver '{ch['name']}'...")
        time.sleep(delay)
        cached = _CACHE.get(ch["url"])
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            _log(f"[warmup] {label} [{i}/{len(channels)}] '{ch['name']}': cache válido, pulando")
            consecutive_fails = 0
            continue
        _log(f"[warmup] {label} [{i}/{len(channels)}] resolvendo '{ch['name']}'...")
        try:
            result = resolve_stream(ch["url"])
            if result.get("streams"):
                _log(f"[warmup] {label} [{i}/{len(channels)}] '{ch['name']}': ok")
                consecutive_fails = 0
            else:
                _log(f"[warmup] {label} [{i}/{len(channels)}] '{ch['name']}': falhou")
                failed.append(ch)
                consecutive_fails += 1
        except Exception as e:
            _log(f"[warmup] {label} [{i}/{len(channels)}] '{ch['name']}': erro - {e}")
            failed.append(ch)
            consecutive_fails += 1

        if consecutive_fails >= _WARMUP_CONSECUTIVE_FAIL and i < len(channels):
            _log(f"[warmup] {label} {consecutive_fails} falhas consecutivas — possível bloqueio de IP. Pausando {_WARMUP_COOLDOWN}s...")
            time.sleep(_WARMUP_COOLDOWN)
            consecutive_fails = 0

    return failed


_WARMUP_RETRY_DELAY = 600  # 10 minutos


def _warmup_all_channels():
    channels = _parse_channels()
    failed = _warmup_pass(channels, "passe 1")

    if failed:
        _log(f"[warmup] {len(failed)} canal(is) falharam. Aguardando {_WARMUP_RETRY_DELAY}s para segundo passe...")
        time.sleep(_WARMUP_RETRY_DELAY)
        still_failed = _warmup_pass(failed, "passe 2")
        if still_failed:
            names = ", ".join(ch["name"] for ch in still_failed)
            _log(f"[warmup] concluído com falhas: {names}")
        else:
            _log("[warmup] concluído — todos resolvidos no passe 2.")
    else:
        _log("[warmup] concluído — todos resolvidos no passe 1.")


def _midnight_restart():
    _log("[scheduler] reiniciando app (restart de 04h)...")
    import sys
    os.execv(sys.executable, [sys.executable] + sys.argv)


_WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "false").lower() == "true"


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    tz = pytz.timezone("America/Sao_Paulo")
    scheduler = BackgroundScheduler(timezone=tz)

    if _WARMUP_ENABLED:
        scheduler.add_job(_warmup_all_channels, CronTrigger(hour=7,  minute=0, timezone=tz), id="warmup_7h")
        scheduler.add_job(_warmup_all_channels, CronTrigger(hour=12, minute=0, timezone=tz), id="warmup_12h")
        _log("[scheduler] agendamentos ativos: warmup 07h, 12h | restart 04h (America/Sao_Paulo)")
    else:
        _log("[scheduler] warmup desativado (WARMUP_ENABLED=false) | restart 04h (America/Sao_Paulo)")

    scheduler.add_job(_midnight_restart, CronTrigger(hour=4, minute=0, timezone=tz), id="restart_4h")
    scheduler.start()

    if _WARMUP_ENABLED:
        from scraper import _IS_DEV
        if _IS_DEV:
            print("[scheduler] DEVELOPMENT: iniciando warmup imediato...")
            threading.Thread(target=_warmup_all_channels, daemon=True).start()


if __name__ == "__main__":
    _start_scheduler()
    app.run(host="0.0.0.0", port=PORT, debug=False)
