#!/usr/bin/env python3
import os, re, json, urllib.parse, base64, secrets, time, threading, queue
from collections import deque
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, send_file
from flask_cors import CORS
from flask_sock import Sock
import requests as http_req

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from scraper import (
    resolve_stream, _latest_valid, _evict_cache, _evict_url,
    get_stream_pool, pool_size, _log, _CACHE_TTL, _MIN_POOL_SIZE,
    is_stream_alive, is_stream_definitely_dead, _channel_hash,
)

import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

app = Flask(__name__)
CORS(app)
sock = Sock(app)

_RATE_EXEMPT = {"/proxy/ts", "/sw.js", "/favicon.ico", "/manifest.json"}

# ── WebSocket sync ────────────────────────────────────────────────────────────
# relay faz broadcast do segmento canônico para todos os clients conectados
_WS_CHANNELS: dict[str, list] = {}   # slug → [ws, ...]
_WS_CHANNELS_LOCK = threading.Lock()

@app.before_request
def global_rate_limit():
    if request.path in _RATE_EXEMPT or request.path.startswith(("/static/", "/ws/")):
        return
    if _rate_check(_client_ip()):
        return jsonify({"error": "rate_limit"}), 429

_ACTIVE_IPS: dict[str, tuple[float, float, str]] = {}  # ip -> (first_seen, last_seen, channel)
_ACTIVE_LOCK = threading.Lock()
_IP_TTL = 30

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


def _ch_slug(channel_url: str) -> str:
    return channel_url.rstrip("/").split("/")[-1]


def _slug_to_channel_url(slug: str) -> str | None:
    for ch in _parse_channels():
        if _ch_slug(ch["url"]) == slug:
            return ch["url"]
    return None


@app.route("/channels")
def channels():
    return jsonify([
        {"name": ch["name"], "slug": _ch_slug(ch["url"])}
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
                    "provider":     embed["provider"],
                    "channel_name": ch["name"],
                    "channel_slug": _ch_slug(ch["url"]),
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

_RESOLVE_STATUS: dict[str, str] = {}  # slug -> "loading" | "ready" | "error"
_RESOLVE_LOCK = threading.Lock()


@app.route("/resolve/<slug>")
def resolve_start(slug):
    channel_url = _slug_to_channel_url(slug)
    if not channel_url:
        _log(f"[resolve] slug desconhecido: {slug}")
        return jsonify({"status": "error"}), 404

    if _latest_valid(channel_url):
        _RESOLVE_STATUS[slug] = "ready"
        _get_relay(channel_url)
        return jsonify({"status": "ready"})

    with _RESOLVE_LOCK:
        if _RESOLVE_STATUS.get(slug) == "loading":
            return jsonify({"status": "loading"})
        _RESOLVE_STATUS[slug] = "loading"

    def _bg():
        try:
            result = resolve_stream(channel_url)
            if result.get("streams"):
                _RESOLVE_STATUS[slug] = "ready"
                _get_relay(channel_url)
            else:
                _log(f"[resolve] sem streams para '{slug}'")
                _RESOLVE_STATUS[slug] = "error"
        except Exception as e:
            _log(f"[resolve] erro ao resolver '{slug}': {e}")
            _RESOLVE_STATUS[slug] = "error"

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "loading"})


@app.route("/resolve/status/<slug>")
def resolve_status(slug):
    return jsonify({"status": _RESOLVE_STATUS.get(slug, "unknown")})


# ── Proxy headers (usados pelo relay e pelo proxy de segmentos) ───────────────

_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


# ── Stream Relay ──────────────────────────────────────────────────────────────
# Um relay por canal ativo: loop de background busca M3U8 do CDN a cada 2s,
# cacheia a versão reescrita (segmentos apontam para /proxy/ts) e serve
# instantaneamente para o HLS.js — sem latência de CDN no caminho crítico.

_RELAY_POLL     = 2.0   # segundos entre fetches do M3U8
_RELAY_STALE    = 15.0  # sem fetch válido por N segundos → M3U8 stale
_RELAY_FAIL_MAX = 3     # falhas consecutivas → troca de fonte
_RELAY_IDLE_TTL = 300   # sem acesso por N segundos → relay para
_RELAY_WINDOW      = 6  # segmentos acumulados na janela (~12s a 2s/seg)
_LIVE_SYNC_COUNT   = 5  # liveSyncDurationCount p/ HLS.js — 1 seg a menos que a janela

_RELAYS: dict[str, "StreamRelay"] = {}
_RELAY_LOCK = threading.Lock()

# ── Segment cache ─────────────────────────────────────────────────────────────
# Segmentos pre-buscados pelo relay; todos os clients servidos do mesmo cache.
_SEG_CACHE: dict[str, tuple[float, bytes]] = {}  # cdn_url -> (ts, data)
_SEG_CACHE_LOCK = threading.Lock()
_SEG_TTL = 35  # segundos — janela de 12s + margem generosa


def _seg_get(url: str) -> bytes | None:
    with _SEG_CACHE_LOCK:
        entry = _SEG_CACHE.get(url)
    if entry and time.time() - entry[0] < _SEG_TTL:
        return entry[1]
    return None


def _seg_put(url: str, data: bytes) -> None:
    now = time.time()
    with _SEG_CACHE_LOCK:
        _SEG_CACHE[url] = (now, data)
        expired = [k for k, (ts, _) in _SEG_CACHE.items() if now - ts > _SEG_TTL]
        for k in expired:
            del _SEG_CACHE[k]


class StreamRelay:
    """Mantém um loop de background que busca e cacheia o M3U8 de um canal."""

    def __init__(self, channel_url: str):
        self._channel_url  = channel_url
        self._active: dict | None = None   # stream entry em uso
        self._m3u8: str | None    = None   # M3U8 reescrito e cacheado
        self._fetched_at: float   = 0.0    # timestamp do último fetch ok
        self._fail_count: int     = 0
        self._sync: dict | None   = None   # {"seq": N, "dur": 2.0} — posição canônica
        self._seg_history: deque  = deque(maxlen=_RELAY_WINDOW)  # janela de segmentos acumulados
        self._lock  = threading.Lock()
        self._ready = threading.Event()    # setado na 1ª vez que M3U8 é cacheado
        self._stop  = threading.Event()
        self.running     = False
        self.last_access = time.time()

    def start(self):
        self.running = True
        threading.Thread(
            target=self._run, daemon=True,
            name=f"relay-{self._channel_url.rstrip('/').split('/')[-1]}",
        ).start()

    def stop(self):
        self.running = False
        self._stop.set()

    def get_m3u8(self, timeout: float = 5.0) -> str | None:
        self.last_access = time.time()
        with self._lock:
            if self._m3u8 and (time.time() - self._fetched_at) < _RELAY_STALE:
                return self._m3u8
        self._ready.wait(timeout=timeout)
        with self._lock:
            if self._m3u8 and (time.time() - self._fetched_at) < _RELAY_STALE:
                return self._m3u8
        return None

    def _pick_source(self) -> dict | None:
        pool = get_stream_pool(self._channel_url)
        if not pool:
            try:
                pool = resolve_stream(self._channel_url).get("streams", [])
            except Exception:
                return None
        if not pool:
            return None
        if self._active and len(pool) > 1:
            others = [e for e in pool if e.get("url") != self._active.get("url")]
            if others:
                return others[0]
        return pool[0]

    _SEQ_RE    = re.compile(r'#EXT-X-MEDIA-SEQUENCE:(\d+)')
    _EXTINF_RE = re.compile(r'#EXTINF:([\d.]+)')

    def _fetch(self, source: dict) -> str | None:
        m3u8_url = source["url"]
        referer  = source.get("referer", "")
        headers  = dict(_PROXY_HEADERS)
        if referer:
            headers["Referer"] = referer
            headers["Origin"]  = referer.rstrip("/").rsplit("/", 1)[0]
        try:
            r = http_req.get(m3u8_url, headers=headers, timeout=10)
            r.raise_for_status()
            fetch_time = time.time()
            raw_lines  = r.text.splitlines()

            seq_m  = self._SEQ_RE.search(r.text)
            dur_m  = self._EXTINF_RE.search(r.text)
            if not seq_m or not dur_m:
                return None

            base_seq = int(seq_m.group(1))
            seg_dur  = float(dur_m.group(1))

            # extrai segmentos novos desta fetch
            new_segs   = []
            new_cdns   = []
            seg_idx    = 0
            for line in raw_lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    cdn_url = stripped if stripped.startswith("http") else urllib.parse.urljoin(m3u8_url, stripped)
                    seq_num = base_seq + seg_idx
                    # só acumula se este seq ainda não está no histórico
                    known_seqs = {s["seq"] for s in self._seg_history}
                    if seq_num not in known_seqs:
                        proxy_url = "/proxy/ts?url=" + _encrypt_url(cdn_url)
                        self._seg_history.append({"seq": seq_num, "dur": seg_dur, "proxy": proxy_url})
                        if _seg_get(cdn_url) is None:
                            new_cdns.append(cdn_url)
                    seg_idx += 1

            if new_cdns:
                threading.Thread(
                    target=self._prefetch, args=(new_cdns, headers), daemon=True
                ).start()

            # atualiza sync com live edge atual
            self._sync = {
                "seq":       base_seq + seg_idx,
                "dur":       seg_dur,
                "wall_ts":   fetch_time - 2 * seg_dur,
                "server_ts": fetch_time,
            }

            return self._build_m3u8(seg_dur, fetch_time)

        except http_req.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                _log(f"[relay] 404 permanente, removendo do pool: hash={_channel_hash(m3u8_url)}")
                _evict_url(self._channel_url, m3u8_url)
                self._active = None
        except Exception:
            pass
        return None

    def _build_m3u8(self, seg_dur: float, fetch_time: float) -> str:
        segs = list(self._seg_history)
        if not segs:
            return None
        first_seq = segs[0]["seq"]
        # PDT para o segmento mais antigo da janela
        oldest_wall = fetch_time - len(segs) * seg_dur
        dt  = datetime.fromtimestamp(oldest_wall, tz=timezone.utc)
        pdt = ("#EXT-X-PROGRAM-DATE-TIME:"
               + dt.strftime("%Y-%m-%dT%H:%M:%S.")
               + f"{dt.microsecond // 1000:03d}Z")
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{int(seg_dur) + 1}",
            f"#EXT-X-MEDIA-SEQUENCE:{first_seq}",
            pdt,
        ]
        for s in segs:
            lines.append(f"#EXTINF:{s['dur']:.3f},")
            lines.append(s["proxy"])
        return "\n".join(lines)

    def _broadcast(self) -> None:
        if not self._sync:
            return
        slug = self._channel_url.rstrip('/').split('/')[-1]
        with _WS_CHANNELS_LOCK:
            entries = list(_WS_CHANNELS.get(slug, []))

        msg = json.dumps(self._sync)
        for e in entries:
            try:
                e["q"].put_nowait(msg)
            except queue.Full:
                pass  # client lento/morto — handler vai limpar

    def _prefetch(self, urls: list, headers: dict) -> None:
        for url in urls:
            if _seg_get(url) is not None:
                continue
            try:
                r = http_req.get(url, headers=headers, timeout=15)
                r.raise_for_status()
                _seg_put(url, r.content)
            except Exception:
                pass

    def _run(self):
        while self.running:
            if self._active is None or self._fail_count >= _RELAY_FAIL_MAX:
                src = self._pick_source()
                if src is None:
                    _log(f"[relay] sem fonte para {self._channel_url.rstrip('/').split('/')[-1]}, aguardando 10s")
                    self._stop.wait(timeout=10)
                    continue
                if self._active is None or src.get("url") != self._active.get("url"):
                    _log(f"[relay] nova fonte: hash={_channel_hash(src['url'])}")
                self._active     = src
                self._fail_count = 0

            body = self._fetch(self._active)
            if body:
                with self._lock:
                    self._m3u8       = body
                    self._fetched_at = time.time()
                    self._fail_count = 0
                self._ready.set()
                self._broadcast()
            else:
                self._fail_count += 1
                _log(f"[relay] falha {self._fail_count}/{_RELAY_FAIL_MAX} hash={_channel_hash(self._active['url']) if self._active else '?'}")

            # mid-tick: re-broadcast sem refetch para reduzir delay de entrada
            if self._stop.wait(timeout=_RELAY_POLL / 2):
                break
            self._broadcast()
            self._stop.wait(timeout=_RELAY_POLL / 2)


def _get_relay(channel_url: str) -> StreamRelay:
    with _RELAY_LOCK:
        relay = _RELAYS.get(channel_url)
        if relay is None or not relay.running:
            relay = StreamRelay(channel_url)
            relay.start()
            _RELAYS[channel_url] = relay
            _log(f"[relay] iniciado: {channel_url.rstrip('/').split('/')[-1]}")
        return relay


def _relay_cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with _RELAY_LOCK:
            stale = [url for url, r in _RELAYS.items() if now - r.last_access > _RELAY_IDLE_TTL]
            for url in stale:
                _RELAYS[url].stop()
                del _RELAYS[url]
                _log(f"[relay] cleanup idle: {url.rstrip('/').split('/')[-1]}")


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


@app.route("/<slug>")
def stream(slug):
    channel_url = _slug_to_channel_url(slug)
    if not channel_url:
        _log(f"[stream] slug desconhecido: {slug}")
        return "canal não encontrado", 404

    ip = _client_ip()
    now = time.time()
    channel_name = next((c["name"] for c in _parse_channels() if c["url"] == channel_url), slug)
    with _ACTIVE_LOCK:
        existing = _ACTIVE_IPS.get(ip)
        is_new = existing is None or (now - existing[1]) >= _IP_TTL
        first_seen = now if is_new else existing[0]
        _ACTIVE_IPS[ip] = (first_seen, now, channel_name)
        if is_new:
            active = sum(1 for fs, ls, _ in _ACTIVE_IPS.values() if now - ls < _IP_TTL)
            ts = datetime.now().strftime("%d/%m %H:%M:%S")
            print(f"[{ts}] Novo IP ({ip}) conectado em '{channel_name}'. Total ativos: ({active}).")

    relay = _get_relay(channel_url)
    m3u8 = relay.get_m3u8(timeout=5.0)
    if m3u8 is None:
        _log(f"[stream] relay sem M3U8 disponível para {slug}")
        return "stream indisponível", 502

    return Response(
        m3u8,
        mimetype="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
    )



@sock.route("/ws/<slug>")
def ws_sync(ws, slug):
    channel_url = _slug_to_channel_url(slug)
    if not channel_url:
        return
    q     = queue.Queue(maxsize=4)
    entry = {"q": q}
    with _WS_CHANNELS_LOCK:
        _WS_CHANNELS.setdefault(slug, []).append(entry)
    # não envia _sync imediatamente — cliente aguarda o próximo broadcast natural
    # do relay, recebendo o "go" no mesmo ciclo que todos os outros clientes

    _SENTINEL = object()

    def _reader():
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
        except Exception:
            pass
        finally:
            q.put_nowait(_SENTINEL)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    try:
        while True:
            try:
                msg = q.get(timeout=20)
            except queue.Empty:
                ws.send('{"ping":true}')
                continue
            if msg is _SENTINEL:
                break
            ws.send(msg)
    except Exception:
        pass
    finally:
        with _WS_CHANNELS_LOCK:
            try: _WS_CHANNELS[slug].remove(entry)
            except ValueError: pass


@app.route("/proxy/ts")
def proxy_ts():
    raw = request.args.get("url", "").strip()
    if not raw:
        _log("[proxy/ts] requisição sem url")
        return "url obrigatória", 400
    try:
        url = _decrypt_url(raw)
    except Exception:
        _log("[proxy/ts] falha ao descriptografar url")
        return "url inválida", 400
    cached = _seg_get(url)
    if cached:
        return Response(
            cached,
            mimetype="video/mp2t",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=20"},
        )
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
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=20", "X-Accel-Buffering": "no"},
        )
    except Exception as e:
        _log(f"[proxy/ts] erro ao buscar segmento: {e}")
        return str(e), 502



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
            {"ip": ip, "connected_for": _fmt_duration(now - fs), "channel": ch}
            for ip, (fs, ls, ch) in _ACTIVE_IPS.items()
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
  table{{border-collapse:collapse;width:100%;max-width:520px}}
  td{{padding:9px 12px;border-bottom:1px solid #1e1e1e;font-size:14px}}
  .ip{{color:#eee}}
  .ch{{color:#4caf50}}
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
  <thead><tr><td><b>IP</b></td><td><b>canal</b></td><td style="text-align:right"><b>conectado há</b></td></tr></thead>
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
      ? d.clients.map(c => `<tr><td class="ip">${{c.ip}}</td><td class="ch">${{c.channel}}</td><td class="dur">${{c.connected_for}}</td></tr>`).join('')
      : '<tr><td colspan="3" class="empty">Nenhum dispositivo ativo</td></tr>';
  }}

  refresh();
  setInterval(refresh, 5000);
</script>
</body></html>""", 200, {"Content-Type": "text/html"}

    return jsonify({"devices": len(active), "clients": active})


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
        if r["pool"] >= _MIN_POOL_SIZE: return "ok"
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
        hls_buffer_max=int(os.environ.get("HLS_MAX_BUFFER_LENGTH", 60)),
        relay_window=_RELAY_WINDOW,
        live_sync_count=_LIVE_SYNC_COUNT)

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
                if is_stream_definitely_dead(s_url):
                    dead.append(s_url)
            for s_url in dead:
                _log(f"[warmup] {label} [{i}/{total}] '{ch['name']}': URL morta confirmada (3/3 falhas), removendo do pool")
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
    import sys, subprocess
    _log("[scheduler] reiniciando app (restart de 04h)...")
    # Novo processo aguarda 3s (tempo do OS liberar a porta) antes de subir
    cmd = f"fuser -k 5000/tcp 2>/dev/null; sleep 2 && exec {sys.executable} {' '.join(sys.argv)}"
    env = os.environ.copy()
    env["SIMULATE_RESTART"] = "false"  # evita loop infinito após restart
    subprocess.Popen(cmd, shell=True, start_new_session=True, env=env)
    os._exit(0)


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
    if os.environ.get("SIMULATE_RESTART", "false").lower() == "true":
        _log("[dev] SIMULATE_RESTART=true — reiniciando em 10s")
        threading.Timer(10, _midnight_restart).start()
    threading.Thread(target=_relay_cleanup_loop, daemon=True).start()
    _start_scheduler()
    app.run(host="0.0.0.0", port=PORT, debug=False)
