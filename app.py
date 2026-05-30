#!/usr/bin/env python3
import os, urllib.parse, base64, secrets, time, threading, queue
from collections import deque
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, send_file
from flask_cors import CORS
import requests as http_req

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from scraper import resolve_stream, _latest_valid, _evict_cache, _log

import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

app = Flask(__name__)
CORS(app)

_RATE_EXEMPT = {"/proxy/ts", "/sw.js", "/favicon.ico", "/manifest.json"}

@app.before_request
def global_rate_limit():
    if request.path in _RATE_EXEMPT or request.path.startswith("/static/"):
        return
    if _rate_check(_client_ip()):
        return jsonify({"error": "rate_limit"}), 429

_ACTIVE_IPS: dict[str, tuple[float, float]] = {}  # ip -> (first_seen, last_seen)
_ACTIVE_LOCK = threading.Lock()
_IP_TTL = 30
_STATUS_SUBSCRIBERS: list[queue.Queue] = []
_STATUS_LOCK = threading.Lock()

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
PORT          = int(os.environ.get("PORT", 5000))
PROXY_SECRET  = os.environ.get("PROXY_SECRET") or secrets.token_hex(16)
STATUS_TOKEN  = os.environ.get("STATUS_TOKEN", "")  # se vazio, /status é aberto

# rate limiter: ip -> deque de timestamps
_RATE_DATA: dict[str, deque] = {}
_RATE_LOCK = threading.Lock()
_RATE_WINDOW = 60    # segundos
_RATE_LIMIT  = 120  # máx requisições por janela


def _rate_check(ip: str) -> bool:
    """Retorna True se a requisição deve ser bloqueada."""
    now = time.time()
    with _RATE_LOCK:
        dq = _RATE_DATA.setdefault(ip, deque())
        while dq and now - dq[0] > _RATE_WINDOW:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            return True
        dq.append(now)
        return False


def _status_auth() -> bool:
    """Retorna True se a requisição tem autorização para acessar /status."""
    if not STATUS_TOKEN:
        return True
    token = request.args.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    return token == STATUS_TOKEN

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
    candidates = [ch for ch in channel_list if p.startswith(_normalize(ch["name"]))]
    if candidates:
        return max(candidates, key=lambda ch: len(ch["name"]))
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
    used_channels: set[str] = set()

    for game in data.get("data", []):
        seen_in_game: set[str] = set()
        first_embed = None
        for embed in game.get("embeds", []):
            ch = _match_channel(embed.get("provider", ""), channel_list)
            if not ch:
                continue
            key = _normalize(ch["name"])
            if key in seen_in_game or key in used_channels:
                continue
            seen_in_game.add(key)
            if first_embed is None:
                first_embed = {
                    "provider":    embed["provider"],
                    "channel_name": ch["name"],
                    "channel_url": _encrypt_url(ch["url"]),
                }

        if first_embed is None:
            continue

        used_channels.add(_normalize(first_embed["channel_name"]))

        result.append({
            "id":          game["id"],
            "title":       game["title"],
            "description": game.get("description", ""),
            "poster":      game.get("poster", ""),
            "start_time":  game.get("start_time", "")[:16],
            "end_time":    game.get("end_time", "")[:16],
            "embeds":      [first_embed],
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

    if _latest_valid(channel_url):
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
    now = time.time()
    with _ACTIVE_LOCK:
        existing = _ACTIVE_IPS.get(ip)
        is_new = existing is None or (now - existing[1]) >= _IP_TTL
        first_seen = now if is_new else existing[0]
        _ACTIVE_IPS[ip] = (first_seen, now)
        if is_new:
            active = sum(1 for fs, ls in _ACTIVE_IPS.values() if now - ls < _IP_TTL)
            ts = datetime.now().strftime("%d/%m %H:%M:%S")
            print(f"[{ts}] Novo IP ({ip}) conectado. Total ativos: ({active}).")
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
                line = "/proxy/ts?url=" + _encrypt_url(seg)
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
    raw = request.args.get("url", "").strip()
    if not raw:
        return "url obrigatória", 400
    try:
        url = _decrypt_url(raw)
    except Exception:
        return "url inválida", 400
    try:
        r = http_req.get(url, headers=_PROXY_HEADERS, timeout=20, stream=True)
        r.raise_for_status()

        def generate():
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    yield chunk
            except Exception:
                return

        return Response(
            generate(),
            mimetype="video/mp2t",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=30", "X-Accel-Buffering": "no"},
        )
    except Exception as e:
        return str(e), 502


def _push_status(count: int):
    with _STATUS_LOCK:
        for q in _STATUS_SUBSCRIBERS:
            q.put_nowait(count)


def _expiry_watcher():
    """Monitora IPs expirados e notifica SSE quando o count muda."""
    last_count = -1
    while True:
        time.sleep(10)
        now = time.time()
        with _ACTIVE_LOCK:
            count = sum(1 for fs, ls in _ACTIVE_IPS.values() if now - ls < _IP_TTL)
        if count != last_count:
            last_count = count
            _push_status(count)



def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


@app.route("/status")
def status():
    if not _status_auth():
        return jsonify({"error": "unauthorized"}), 401
    now = time.time()
    with _ACTIVE_LOCK:
        active = [
            {"ip": ip, "connected_for": _fmt_duration(now - fs)}
            for ip, (fs, ls) in _ACTIVE_IPS.items()
            if now - ls < _IP_TTL
        ]
    return jsonify({"devices": len(active), "clients": active})


@app.route("/status/stream")
def status_stream():
    q = queue.Queue()
    with _STATUS_LOCK:
        _STATUS_SUBSCRIBERS.append(q)

    def generate():
        now = time.time()
        with _ACTIVE_LOCK:
            count = sum(1 for fs, ls in _ACTIVE_IPS.values() if now - ls < _IP_TTL)
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

_GIT_HASH = os.popen("git rev-parse --short HEAD").read().strip() or "0"

@app.route("/")
def index():
    return render_template("player.html", v=_GIT_HASH)

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
_WARMUP_WORKERS = 1           # serial por padrão — paralelo aumenta suspeita no mesmo IP


def _warmup_pass(channels: list, label: str) -> list:
    """Resolve canais serialmente. Retorna os que falharam."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    channels = list(channels)
    random.shuffle(channels)
    total = len(channels)
    _log(f"[warmup] {label}: {total} canais ({_WARMUP_WORKERS} workers)...")

    wfc: dict[int, int] = {}   # thread ident → contagem de falhas consecutivas
    wfc_lock = threading.Lock()

    def _resolve_one(item):
        i, ch = item
        tid = threading.current_thread().ident
        time.sleep(random.uniform(7, 17))

        hit = _latest_valid(ch["url"])
        if hit:
            stream_url = (hit[1].get("streams") or [{}])[0].get("url", "")
            alive = False
            if stream_url:
                try:
                    r = http_req.head(stream_url, timeout=3, allow_redirects=True)
                    alive = r.status_code < 400
                except Exception:
                    pass
            if alive:
                _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': URL viva, pulando")
                with wfc_lock:
                    wfc[tid] = 0
                return None
            age_h = (time.time() - hit[0]) / 3600
            _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': URL morta (cache {age_h:.1f}h), re-resolvendo...")
            _evict_cache(ch["url"])

        _log(f"[warmup] {label} [{i}/{total}] resolvendo '{ch['name']}'...")
        ok = False
        try:
            ok = bool(resolve_stream(ch["url"]).get("streams"))
        except Exception as e:
            _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': erro - {e}")

        if ok:
            _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': ok")
            with wfc_lock:
                wfc[tid] = 0
            return None

        _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': falhou")
        with wfc_lock:
            wfc[tid] = wfc.get(tid, 0) + 1
            consec = wfc[tid]
        if consec >= _WARMUP_CONSECUTIVE_FAIL:
            _log(f"[warmup] {label} worker — {consec} falhas consecutivas, pausando {_WARMUP_COOLDOWN}s...")
            time.sleep(_WARMUP_COOLDOWN)
            with wfc_lock:
                wfc[tid] = 0
        return ch

    with ThreadPoolExecutor(max_workers=_WARMUP_WORKERS) as pool:
        return [ch for ch in pool.map(_resolve_one, enumerate(channels, 1)) if ch is not None]


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
    os._exit(0)  # hard-exit: systemd reinicia o processo; os.execv herdava o socket e conflitava com a porta


_WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "false").lower() == "true"


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    tz = pytz.timezone("America/Sao_Paulo")
    scheduler = BackgroundScheduler(timezone=tz)

    if _WARMUP_ENABLED:
        scheduler.add_job(_warmup_all_channels, CronTrigger(hour=7,  minute=0, timezone=tz), id="warmup_7h")
        scheduler.add_job(_warmup_all_channels, CronTrigger(hour=13, minute=0, timezone=tz), id="warmup_13h")
        _log("[scheduler] agendamentos ativos: warmup 07h, 13h | restart 04h (America/Sao_Paulo)")
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
    threading.Thread(target=_expiry_watcher, daemon=True).start()
    _start_scheduler()
    app.run(host="0.0.0.0", port=PORT, debug=False)
