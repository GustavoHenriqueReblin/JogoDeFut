import os, re, asyncio, time, threading, sys, json
import requests as _http
from patchright.async_api import async_playwright

_IS_DEV = os.environ.get("ENVIRONMENT", "DEVELOPMENT").upper() != "PRODUCTION"

def _log(msg: str):
    if _IS_DEV:
        from datetime import datetime
        try:
            print(f"[{datetime.now().strftime('%d/%m %H:%M:%S')}] {msg}", flush=True)
        except Exception:
            pass

_CLOUDFLAIRE_PLAYERS = {
    k: v
    for entry in os.environ.get("CLOUDFLAIRE_PLAYERS", "").split(",")
    if ":" in entry
    for k, v in [entry.split(":", 1)]
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"}
_HEADLESS = os.environ.get("HEADLESS_DEBUG", "").lower() != "true"

# player_url -> [(timestamp, stream_entry), ...]
# stream_entry = {"url": "...", "referer": "..."}
_CACHE: dict[str, list[tuple[float, dict]]] = {}
_CACHE_TTL = 172800       # 48h — TTL confirmado >24h, margem de segurança
_MIN_POOL_SIZE = int(os.environ.get("MIN_POOL_SIZE", 5))
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_META = threading.Lock()

_BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
_CACHE_FILE    = os.path.join(_BASE_DIR, "cache.json")
_STREAM_LOG    = os.path.join(_BASE_DIR, "stream_log.txt")
_CACHE_WRITE_LOCK = threading.Lock()


def _load_cache():
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = 0
        for url, value in data.items():
            # novo formato: [[ts, entry], ...]
            if isinstance(value, list) and value and isinstance(value[0], list):
                _CACHE[url] = [(float(ts), entry) for ts, entry in value]
                count += len(_CACHE[url])
            # formato antigo: [ts, {"streams": [...]}]
            elif isinstance(value, list) and len(value) == 2 and isinstance(value[0], (int, float)):
                ts, result = value
                streams = result.get("streams", []) if isinstance(result, dict) else []
                if streams:
                    _CACHE[url] = [(float(ts), streams[0])]
                    count += 1
        _log(f"[cache] {len(data)} canais, {count} URLs carregadas do disco")
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"[cache] erro ao carregar cache do disco: {e}")


def _save_cache():
    now = time.time()
    try:
        with _CACHE_WRITE_LOCK:
            valid = {}
            for url, pool in _CACHE.items():
                entries = [[ts, entry] for ts, entry in pool if now - ts < _CACHE_TTL]
                if entries:
                    valid[url] = entries
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(valid, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log(f"[cache] erro ao salvar cache no disco: {e}")


def _append_stream_log(player_url: str, stream_entry: dict):
    try:
        from datetime import datetime
        line = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{player_url} | {stream_entry['url']} | referer: {stream_entry.get('referer', '')}\n"
        )
        with _CACHE_WRITE_LOCK:
            with open(_STREAM_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


_load_cache()


# ── Stream validation ─────────────────────────────────────────────────────────

def is_stream_alive(stream_url: str) -> bool:
    """Valida se uma URL de stream HLS está viva e com P2P disponível."""
    try:
        r = _http.get(stream_url, timeout=5, allow_redirects=True)
        if r.status_code >= 400:
            return False
        body = r.text
        if not body.lstrip().startswith("#EXTM3U"):
            return False
        if "#EXT-X-ENDLIST" in body:
            return False
        p2p_png = next(
            (l.strip() for l in body.splitlines()
             if "cdn.nossoplayer.site" in l and l.strip().endswith(".png")),
            None,
        )
        if p2p_png:
            try:
                pr = _http.head(p2p_png, timeout=5, allow_redirects=True)
                if pr.status_code == 404:
                    return False
            except Exception:
                pass  # erro de rede → assume P2P ok
        return True
    except Exception:
        return False


# ── Pool helpers ──────────────────────────────────────────────────────────────

def _valid_pool(player_url: str) -> list[tuple[float, dict]]:
    """Retorna todas as entradas não expiradas do pool."""
    now = time.time()
    return [(ts, e) for ts, e in _CACHE.get(player_url, []) if now - ts < _CACHE_TTL]


def _latest_valid(player_url: str) -> tuple[float, dict] | None:
    """Retorna a entrada mais recente do pool, ou None se vazio/expirado."""
    pool = _valid_pool(player_url)
    return max(pool, key=lambda x: x[0]) if pool else None


def get_stream_pool(player_url: str) -> list[dict]:
    """Retorna todas as stream entries válidas do pool."""
    return [e for _, e in _valid_pool(player_url)]


def pool_size(player_url: str) -> int:
    return len(_valid_pool(player_url))


def _evict_cache(player_url: str):
    """Remove todo o pool do canal (força re-resolve com Chromium)."""
    _CACHE.pop(player_url, None)
    threading.Thread(target=_save_cache, daemon=True).start()


def _evict_url(player_url: str, stream_url: str):
    """Remove uma URL específica do pool sem descartar as demais."""
    pool = _CACHE.get(player_url)
    if not pool:
        return
    _CACHE[player_url] = [(ts, e) for ts, e in pool if e.get("url") != stream_url]
    if not _CACHE[player_url]:
        del _CACHE[player_url]
    threading.Thread(target=_save_cache, daemon=True).start()


# ── Scraping ──────────────────────────────────────────────────────────────────

async def _scrape_token(page_url: str) -> str | None:
    token = None

    def _make_interceptors(page):
        async def intercept(request):
            nonlocal token
            if "challenges.cloudflare.com/turnstile" in request.url and "token=" in request.url:
                m = re.search(r"token=([^&]+)", request.url)
                if m:
                    t = m.group(1)
                    if t.count('.') >= 2 and len(t) > 100:
                        token = t
                        _log(f"[scraper] token capturado via URL len={len(t)}")

        async def intercept_response(response):
            nonlocal token
            if token:
                return
            if "challenges.cloudflare.com" in response.url:
                try:
                    body = await response.text()
                    m = re.search(r'"token"\s*:\s*"([^"]+)"', body)
                    if m:
                        t = m.group(1)
                        if t.count('.') >= 2 and len(t) > 100:
                            token = t
                            _log(f"[scraper] token capturado via response body len={len(t)}")
                except Exception:
                    pass

        page.on("request", intercept)
        page.on("response", intercept_response)

    async def _poll_token(page, attempts=30):
        nonlocal token
        for i in range(attempts):
            if token:
                break
            try:
                t = await page.evaluate("""() => {
                    const el = document.querySelector('[name="cf-turnstile-response"]');
                    return el ? el.value : null;
                }""")
                if t and t.count('.') >= 2 and len(t) > 100:
                    token = t
                    _log(f"[scraper] token capturado via DOM (tentativa {i+1}) len={len(t)}")
                    break
            except Exception as ex:
                _log(f"[scraper] erro ao ler DOM (tentativa {i+1}): {ex}")
            if (i + 1) % 5 == 0:
                _log(f"[scraper] aguardando token... {i+1}/{attempts}s")
            await asyncio.sleep(1)

    _log(f"[scraper] abrindo browser para: {page_url}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=_HEADLESS)
        ctx = await browser.new_context(locale="pt-BR", accept_downloads=False)
        page = await ctx.new_page()
        _make_interceptors(page)
        try:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            title = await page.title()
            _log(f"[scraper] página carregada: {title}")
            await page.bring_to_front()
            await _poll_token(page)
        except Exception as e:
            _log(f"[scraper] erro ao carregar página: {e}")
        finally:
            await ctx.close()
            await browser.close()

    if token:
        _log("[scraper] token obtido com sucesso")
    else:
        print("[scraper] FALHA: token não encontrado após 30s")
    return token


def _get_lock(key: str) -> threading.Lock:
    with _LOCKS_META:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _channel_hash(stream_url: str) -> str | None:
    """Extrai o hash do canal da stream URL (nossoplayer_{hash}/style.css)."""
    m = re.search(r"nossoplayer_([a-f0-9]+)/", stream_url)
    return m.group(1) if m else None


def _accumulate_bg(player_url: str):
    """Tenta acumular mais uma URL no pool em background (sem bloquear o caller)."""
    lock = _get_lock(player_url)
    if not lock.acquire(blocking=True, timeout=90):
        return
    try:
        current_pool = _valid_pool(player_url)
        if len(current_pool) >= _MIN_POOL_SIZE:
            return
        new_entry = _do_resolve(player_url)
        if new_entry:
            if not is_stream_alive(new_entry["url"]):
                _log(f"[scraper] URL resolvida não passou na validação (P2P/M3U8), descartando: {player_url}")
            else:
                now = time.time()
                new_hash = _channel_hash(new_entry["url"])
                existing_hashes = {_channel_hash(e.get("url", "")) for _, e in _CACHE.get(player_url, [])}
                if new_hash and new_hash not in existing_hashes:
                    _CACHE.setdefault(player_url, []).append((now, new_entry))
                    _log(f"[scraper] hash novo adicionado ao pool (total: {pool_size(player_url)}): {player_url}")
                    threading.Thread(target=_save_cache, daemon=True).start()
                    threading.Thread(target=_append_stream_log, args=(player_url, new_entry), daemon=True).start()
                else:
                    _log(f"[scraper] hash duplicado, não adicionado ao pool: {player_url}")
    finally:
        lock.release()


def resolve_stream(player_url: str) -> dict:
    """
    Retorna o pool atual imediatamente. Se pool < MIN_POOL_SIZE, dispara
    acumulação em background. Pool vazio bloqueia até resolver.
    """
    current_pool = _valid_pool(player_url)

    if len(current_pool) >= _MIN_POOL_SIZE:
        ages = [int(time.time() - ts) for ts, _ in current_pool]
        _log(f"[scraper] pool completo ({len(current_pool)} URLs, idades: {ages}s): {player_url}")
        return {"streams": [e for _, e in current_pool]}

    if current_pool:
        # pool parcial — serve o que tem e acumula em background
        _log(f"[scraper] pool parcial ({len(current_pool)}/{_MIN_POOL_SIZE}), acumulando em bg: {player_url}")
        threading.Thread(target=_accumulate_bg, args=(player_url,), daemon=True).start()
        return {"streams": [e for _, e in current_pool]}

    # pool vazio — bloqueia até resolver
    _log(f"[scraper] pool vazio, resolvendo: {player_url}")
    _accumulate_bg(player_url)
    return {"streams": [e for _, e in _valid_pool(player_url)]}


def _do_resolve(player_url: str) -> dict | None:
    """Executa o Chromium e chama a API. Retorna uma stream entry ou None."""
    host = re.search(r"https?://([^/]+)", player_url)
    host = host.group(1) if host else ""
    fonte = _CLOUDFLAIRE_PLAYERS.get(host)
    if not fonte:
        _log(f"[scraper] ERRO: host não mapeado em CLOUDFLAIRE_PLAYERS: {host}")
        return None

    channel = re.search(r"/(?:tv/|embed/|)([^/?#]+)$", player_url)
    if not channel:
        _log(f"[scraper] ERRO: canal não encontrado na URL: {player_url}")
        return None
    channel = channel.group(1)

    _log(f"[scraper] resolvendo canal={channel} fonte={fonte}")

    _MAX_ATTEMPTS = 8
    _RETRY_DELAYS = [3, 5, 8, 10, 12, 15, 20]

    for attempt in range(_MAX_ATTEMPTS):
        _log(f"[scraper] tentativa {attempt+1}/{_MAX_ATTEMPTS} de obter token")
        token = asyncio.run(_scrape_token(player_url))
        if not token:
            _log(f"[scraper] FALHA na tentativa {attempt+1}: sem token")
            return None
        try:
            _log(f"[scraper] POST get_token (tentativa {attempt+1})...")
            r = _http.post(
                "https://api.cloudflaire.lat/get_token",
                headers={
                    "content-type": "application/json",
                    "accept": "*/*",
                    "accept-language": "pt-BR,pt;q=0.9",
                    "user-agent": _UA,
                    "origin": f"https://{host}",
                    "referer": f"https://{host}/",
                    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "cross-site",
                },
                json={"fonte": fonte, "channel": channel, "token": token},
                timeout=15,
            )
            _log(f"[scraper] get_token status={r.status_code} (tentativa {attempt+1}) token_len={len(token)}")
            if r.status_code == 404:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                _log(f"[scraper] API retornou 404 body={r.text[:300]!r} canal={channel} fonte={fonte}, aguardando {delay}s... ({attempt+1}/{_MAX_ATTEMPTS})")
                time.sleep(delay)
                continue
            if r.status_code not in (200, 201):
                _log(f"[scraper] ERRO inesperado da API: status={r.status_code} body={r.text[:200]!r}")
                return None
            url = r.json().get("url")
            if url:
                _log(f"[scraper] stream URL obtida: {url}")
                return {"url": url, "referer": player_url}
            _log(f"[scraper] ERRO: resposta sem campo 'url': {r.text[:200]!r}")
        except Exception as e:
            _log(f"[scraper] ERRO na chamada get_token: {e}")

    _log(f"[scraper] FALHA: todas as {_MAX_ATTEMPTS} tentativas esgotadas")
    return None
