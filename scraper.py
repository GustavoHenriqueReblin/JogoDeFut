import os, re, asyncio, time, threading, sys, json
import requests as _http
from patchright.async_api import async_playwright

_IS_DEV = os.environ.get("ENVIRONMENT", "DEVELOPMENT").upper() != "PRODUCTION"

def _log(msg: str):
    if _IS_DEV:
        from datetime import datetime
        print(f"[{datetime.now().strftime('%d/%m %H:%M:%S')}] {msg}")

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

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 43200  # 12 horas
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_META = threading.Lock()

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")
_CACHE_WRITE_LOCK = threading.Lock()


def _load_cache():
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        loaded = 0
        for url, (ts, result) in data.items():
            if now - ts < _CACHE_TTL:
                _CACHE[url] = (ts, result)
                loaded += 1
        _log(f"[cache] {loaded} entradas carregadas do disco ({len(data) - loaded} expiradas ignoradas)")
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"[cache] erro ao carregar cache do disco: {e}")


def _save_cache():
    try:
        with _CACHE_WRITE_LOCK:
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({url: [ts, result] for url, (ts, result) in _CACHE.items()}, f)
    except Exception as e:
        _log(f"[cache] erro ao salvar cache no disco: {e}")


_load_cache()


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


def resolve_stream(player_url: str) -> dict:
    cached = _CACHE.get(player_url)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        age = int(time.time() - cached[0])
        _log(f"[scraper] cache hit ({age}s atrás): {player_url}")
        return cached[1]

    lock = _get_lock(player_url)
    if not lock.acquire(blocking=True, timeout=90):
        print(f"[scraper] ERRO: timeout aguardando lock para {player_url}")  # sempre visível
        return {"streams": []}

    try:
        cached = _CACHE.get(player_url)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            age = int(time.time() - cached[0])
            _log(f"[scraper] cache hit pós-lock ({age}s atrás): {player_url}")
            return cached[1]

        result = _do_resolve(player_url)
        if result.get("streams"):
            _CACHE[player_url] = (time.time(), result)
            _log(f"[scraper] resultado salvo em cache por {_CACHE_TTL}s")
            threading.Thread(target=_save_cache, daemon=True).start()
        return result
    finally:
        lock.release()


def _do_resolve(player_url: str) -> dict:
    host = re.search(r"https?://([^/]+)", player_url)
    host = host.group(1) if host else ""
    fonte = _CLOUDFLAIRE_PLAYERS.get(host)
    if not fonte:
        _log(f"[scraper] ERRO: host não mapeado em CLOUDFLAIRE_PLAYERS: {host}")
        return {"streams": []}

    channel = re.search(r"/(?:tv/|embed/|)([^/?#]+)$", player_url)
    if not channel:
        _log(f"[scraper] ERRO: canal não encontrado na URL: {player_url}")
        return {"streams": []}
    channel = channel.group(1)

    _log(f"[scraper] resolvendo canal={channel} fonte={fonte}")

    _MAX_ATTEMPTS = 8
    _RETRY_DELAYS = [3, 5, 8, 10, 12, 15, 20]  # delay após cada 404 (índice = tentativa que falhou)

    for attempt in range(_MAX_ATTEMPTS):
        _log(f"[scraper] tentativa {attempt+1}/{_MAX_ATTEMPTS} de obter token")
        token = asyncio.run(_scrape_token(player_url))
        if not token:
            _log(f"[scraper] FALHA na tentativa {attempt+1}: sem token")
            return {"streams": []}
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
            if r.status_code != 200 and r.status_code != 201:
                _log(f"[scraper] ERRO inesperado da API: status={r.status_code} body={r.text[:200]!r}")
                return {"streams": []}
            url = r.json().get("url")
            if url:
                _log(f"[scraper] stream URL obtida: {url}")
                return {"streams": [{"provider": "HD", "url": url, "referer": player_url}]}
            _log(f"[scraper] ERRO: resposta sem campo 'url': {r.text[:200]!r}")
        except Exception as e:
            _log(f"[scraper] ERRO na chamada get_token: {e}")

    _log(f"[scraper] FALHA: todas as {_MAX_ATTEMPTS} tentativas esgotadas")
    return {"streams": []}
