#!/usr/bin/env python3
import os, urllib.parse, base64, secrets, time, threading, queue
from collections import deque
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, send_file
from flask_cors import CORS
import requests as http_req

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from scraper import (
    resolve_stream, _latest_valid, _evict_cache, _evict_url,
    get_stream_pool, pool_size, _log, _CACHE_TTL, _MIN_POOL_SIZE,
    is_stream_alive,
)

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

    streams = result.get("streams", [])
    if not streams:
        return "stream não encontrado", 404

    total = len(streams)
    for idx, stream_entry in enumerate(streams, 1):
        m3u8_url = stream_entry["url"]
        referer  = stream_entry.get("referer", "")
        from scraper import _channel_hash
        label = _channel_hash(m3u8_url) or m3u8_url

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

            if idx > 1:
                _log(f"[stream] ok [{idx}/{total}] hash={label}")
            return Response(
                "\n".join(lines),
                mimetype="application/vnd.apple.mpegurl",
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
            )
        except http_req.HTTPError:
            _log(f"[stream] morta [{idx}/{total}] {r.status_code} hash={label}, evict → próxima")
            _evict_url(channel_url, m3u8_url)
            continue
        except Exception as e:
            _log(f"[stream] timeout/erro [{idx}/{total}] hash={label}: {e} → próxima")
            _evict_url(channel_url, m3u8_url)
            continue

    _log(f"[stream] todas as {total} URLs falharam para {channel_url}, evictando cache")
    _evict_cache(channel_url)
    return "stream indisponível", 502


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

    if request.headers.get("Accept", "").startswith("text/html"):
        token_qs = f"?token={request.args.get('token', '')}" if request.args.get('token') else ""
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Status</title>
<style>
  body{{font-family:monospace;background:#111;color:#eee;padding:24px;margin:0}}
  h2{{margin-bottom:4px}}
  .sub{{color:#666;font-size:13px;margin-bottom:24px}}
  .counter{{font-size:48px;font-weight:bold;color:#4caf50;margin-bottom:8px;line-height:1}}
  .counter.zero{{color:#555}}
  .label{{font-size:13px;color:#aaa;margin-bottom:24px}}
  table{{border-collapse:collapse;width:100%;max-width:420px}}
  td{{padding:9px 12px;border-bottom:1px solid #1e1e1e;font-size:14px}}
  .ip{{color:#eee}}
  .dur{{color:#aaa;text-align:right}}
  .empty{{color:#555;text-align:center;padding:20px}}
  .dot{{display:inline-block;width:8px;height:8px;border-radius:50%;
        background:#4caf50;margin-right:6px;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
</style></head><body>
<h2>Dispositivos Ativos</h2>
<div class="counter zero" id="counter">—</div>
<div class="label" id="label"></div>
<table>
  <thead><tr><td><b>IP</b></td><td style="text-align:right"><b>conectado há</b></td></tr></thead>
  <tbody id="tbody"><tr><td colspan="2" class="empty">Carregando…</td></tr></tbody>
</table>
<script>
  const TOKEN = '{request.args.get("token", "")}';
  const qs = TOKEN ? '?token=' + TOKEN : '';

  async function refresh() {{
    const r = await fetch('/status' + qs);
    if (!r.ok) return;
    const d = await r.json();
    const n = d.devices;
    document.getElementById('counter').textContent = n;
    document.getElementById('counter').className = 'counter' + (n === 0 ? ' zero' : '');
    const dot = n > 0 ? '<span class="dot"></span>' : '';
    const s = n !== 1 ? 's' : '';
    document.getElementById('label').innerHTML = dot + 'dispositivo' + s + ' ativo' + s;
    document.getElementById('tbody').innerHTML = d.clients.length
      ? d.clients.map(c => `<tr><td class="ip">${{c.ip}}</td><td class="dur">${{c.connected_for}}</td></tr>`).join('')
      : '<tr><td colspan="2" class="empty">Nenhum dispositivo ativo</td></tr>';
  }}

  refresh();
  setInterval(refresh, 5000);
</script>
</body></html>""", 200, {"Content-Type": "text/html"}

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


# ── Cache status ──────────────────────────────────────────────────────────────

@app.route("/cache-status")
def cache_status():
    if not _status_auth():
        return jsonify({"error": "unauthorized"}), 401
    now = time.time()
    rows = []
    dias_semana = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    now_dt = datetime.now()
    for ch in _parse_channels():
        entry = _latest_valid(ch["url"])
        sz = pool_size(ch["url"])
        if entry:
            expires_ts = entry[0] + _CACHE_TTL
            expires_dt = datetime.fromtimestamp(expires_ts)
            if expires_dt.date() == now_dt.date():
                expires_str = f"hoje {expires_dt.strftime('%H:%M')}"
            elif expires_dt.date() == (now_dt + timedelta(days=1)).date():
                expires_str = f"amanhã {expires_dt.strftime('%H:%M')}"
            else:
                expires_str = f"{dias_semana[expires_dt.weekday()]} {expires_dt.strftime('%H:%M')}"
            rows.append({"name": ch["name"], "status": "ok", "expires": expires_str, "pool": sz})
        else:
            rows.append({"name": ch["name"], "status": "miss", "expires": None, "pool": 0})

    def _badge_class(r):
        if r["status"] != "ok": return "miss"
        if r["pool"] >= 3: return "ok"
        return "warn"

    def _badge_text(r):
        if r["status"] != "ok": return "sem cache"
        return f'✓ {r["pool"]} URL{"s" if r["pool"] != 1 else ""} · expira {r["expires"]}'

    html_rows = "".join(
        f'<tr>'
        f'<td><img src="/static/logos/{r["name"]}.webp" onerror="this.style.display=\'none\'" '
        f'style="height:22px;vertical-align:middle;margin-right:8px">{r["name"]}</td>'
        f'<td><span class="badge {_badge_class(r)}">{_badge_text(r)}</span></td>'
        f'</tr>'
        for r in rows
    )
    ok = sum(1 for r in rows if r["status"] == "ok")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Cache Status</title>
<style>
  body{{font-family:monospace;background:#111;color:#eee;padding:24px}}
  h2{{margin-bottom:16px}}
  table{{border-collapse:collapse;width:100%;max-width:480px}}
  td{{padding:8px 12px;border-bottom:1px solid #222}}
  .badge{{padding:3px 10px;border-radius:12px;font-size:13px}}
  .ok{{background:#1a3a1a;color:#4caf50}}
  .warn{{background:#3a2e00;color:#ffc107}}
  .miss{{background:#3a1a1a;color:#f44336}}
  .summary{{margin-bottom:16px;color:#aaa}}
</style></head><body>
<h2>Cache dos Canais</h2>
<p class="summary">{ok}/{len(rows)} com cache válido &nbsp;·&nbsp; TTL 48h &nbsp;·&nbsp; pool mín. {_MIN_POOL_SIZE} URLs</p>
<table>{html_rows}</table>
</body></html>""", 200, {"Content-Type": "text/html"}


# ── Static ────────────────────────────────────────────────────────────────────

_GIT_HASH = os.popen("git rev-parse --short HEAD").read().strip() or "0"

@app.route("/")
def index():
    return render_template("player.html", v=_GIT_HASH,
        hls_buffer=int(os.environ.get("HLS_BUFFER_LENGTH", 30)),
        hls_buffer_max=int(os.environ.get("HLS_MAX_BUFFER_LENGTH", 60)))

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
_WARMUP_WORKERS = int(os.environ.get("WARMUP_WORKERS", 2))


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

        pool = get_stream_pool(ch["url"])
        if pool:
            dead = []
            for entry in pool:
                s_url = entry.get("url", "")
                if not s_url:
                    continue
                if not is_stream_alive(s_url):
                    dead.append(s_url)
            for s_url in dead:
                _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': URL morta, removendo do pool")
                _evict_url(ch["url"], s_url)
            alive_count = pool_size(ch["url"])
            if alive_count >= _MIN_POOL_SIZE:
                _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': pool completo ({alive_count} URLs), pulando")
                with wfc_lock:
                    wfc[tid] = 0
                return None
            if alive_count > 0:
                _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': pool parcial ({alive_count}/{_MIN_POOL_SIZE}), acumulando...")

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
    import sys, time
    _log("[scheduler] reiniciando app (restart de 04h)...")
    time.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


_WARMUP_ENABLED = os.environ.get("WARMUP_ENABLED", "false").lower() == "true"


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    tz = pytz.timezone("America/Sao_Paulo")
    scheduler = BackgroundScheduler(timezone=tz)

    if _WARMUP_ENABLED:
        scheduler.add_job(_warmup_all_channels, CronTrigger(hour=5,  minute=0, timezone=tz), id="warmup_5h")
        scheduler.add_job(_warmup_all_channels, CronTrigger(hour=12, minute=0, timezone=tz), id="warmup_12h")
        scheduler.add_job(_warmup_all_channels, CronTrigger(hour=18, minute=0, timezone=tz), id="warmup_18h")
        _log("[scheduler] agendamentos ativos: warmup 05h, 12h, 18h | restart 04h (America/Sao_Paulo)")
    else:
        _log("[scheduler] warmup desativado (WARMUP_ENABLED=false) | restart 04h (America/Sao_Paulo)")

    scheduler.add_job(_midnight_restart, CronTrigger(hour=4, minute=0, timezone=tz), id="restart_4h")
    scheduler.start()

    from scraper import _IS_DEV
    if _IS_DEV:
        if _WARMUP_ENABLED:
            print("[scheduler] DEVELOPMENT: iniciando warmup imediato...")
            threading.Thread(target=_warmup_all_channels, daemon=True).start()


if __name__ == "__main__":
    threading.Thread(target=_expiry_watcher, daemon=True).start()
    _start_scheduler()
    app.run(host="0.0.0.0", port=PORT, debug=False)
