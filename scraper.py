import os, re, asyncio, time, threading, sys
import requests as _http
from camoufox.async_api import AsyncCamoufox

_orig_unraisable = sys.unraisablehook
def _unraisable_hook(args):
    if isinstance(args.exc_value, RuntimeError) and "Event loop is closed" in str(args.exc_value):
        return
    _orig_unraisable(args)
sys.unraisablehook = _unraisable_hook

_CLOUDFLAIRE_PLAYERS = {
    k: v
    for entry in os.environ.get("CLOUDFLAIRE_PLAYERS", "").split(",")
    if ":" in entry
    for k, v in [entry.split(":", 1)]
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "pt-BR,pt;q=0.9"}
_HEADLESS = os.environ.get("HEADLESS_DEBUG", "").lower() != "true"

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300  # 5 minutos
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_META = threading.Lock()


async def _scrape_token(page_url: str) -> str | None:
    token = None

    def _make_interceptors(page):
        async def intercept(request):
            nonlocal token
            if "challenges.cloudflare.com/turnstile" in request.url and "token=" in request.url:
                m = re.search(r"token=([^&]+)", request.url)
                if m:
                    token = m.group(1)
                    print("[scraper] token capturado via URL")

        async def intercept_response(response):
            nonlocal token
            if token:
                return
            if "challenges.cloudflare.com" in response.url:
                try:
                    body = await response.text()
                    m = re.search(r'"token"\s*:\s*"([^"]+)"', body)
                    if m:
                        token = m.group(1)
                        print("[scraper] token capturado via response body")
                except Exception:
                    pass

        page.on("request", intercept)
        page.on("response", intercept_response)

    async def _poll_token(page, attempts=45):
        nonlocal token
        for i in range(attempts):
            if token:
                break
            try:
                info = await page.evaluate("""() => {
                    const cf = document.querySelector('[name="cf-turnstile-response"]');
                    const frames = document.querySelectorAll('iframe');
                    const widget = document.querySelector('.cf-turnstile, [data-sitekey]');
                    return {
                        token: cf ? cf.value : null,
                        iframes: frames.length,
                        srcs: Array.from(frames).map(f => f.src.slice(0, 100)),
                        hasWidget: !!widget,
                        bodyLen: document.body ? document.body.innerHTML.length : 0,
                    };
                }""")
                if i == 0:
                    print(f"[scraper] diagnóstico: iframes={info.get('iframes')}, hasWidget={info.get('hasWidget')}, bodyLen={info.get('bodyLen')}")
                    for s in info.get('srcs', []):
                        print(f"[scraper]   iframe src: {s}")
                if info.get('token'):
                    token = info['token']
                    print(f"[scraper] token capturado via DOM (tentativa {i+1})")
                    break
            except Exception as ex:
                print(f"[scraper] erro ao ler DOM (tentativa {i+1}): {ex}")
            if (i + 1) % 5 == 0:
                print(f"[scraper] aguardando token {i+1}/{attempts} | iframes={info.get('iframes',0) if 'info' in dir() else '?'}")
            await asyncio.sleep(1)

    print(f"[scraper] abrindo browser para: {page_url}")
    async with AsyncCamoufox(headless=_HEADLESS, locale="pt-BR") as browser:
        page = await browser.new_page()
        _make_interceptors(page)
        try:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            print(f"[scraper] página carregada: {await page.title()}")
            await asyncio.sleep(3)
            await _poll_token(page)
        except Exception as e:
            print(f"[scraper] erro ao carregar página: {e}")

    if token:
        print("[scraper] token obtido com sucesso")
    else:
        print("[scraper] FALHA: token não encontrado após 20s")
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
        print(f"[scraper] cache hit ({age}s atrás): {player_url}")
        return cached[1]

    lock = _get_lock(player_url)
    if not lock.acquire(blocking=True, timeout=90):
        print(f"[scraper] ERRO: timeout aguardando lock para {player_url}")
        return {"streams": []}

    try:
        cached = _CACHE.get(player_url)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            age = int(time.time() - cached[0])
            print(f"[scraper] cache hit pós-lock ({age}s atrás): {player_url}")
            return cached[1]

        result = _do_resolve(player_url)
        if result.get("streams"):
            _CACHE[player_url] = (time.time(), result)
            print(f"[scraper] resultado salvo em cache por {_CACHE_TTL}s")
        return result
    finally:
        lock.release()


def _do_resolve(player_url: str) -> dict:
    host = re.search(r"https?://([^/]+)", player_url)
    host = host.group(1) if host else ""
    fonte = _CLOUDFLAIRE_PLAYERS.get(host)
    if not fonte:
        print(f"[scraper] ERRO: host não mapeado em CLOUDFLAIRE_PLAYERS: {host}")
        return {"streams": []}

    channel = re.search(r"/(?:tv/|embed/|)([^/?#]+)$", player_url)
    if not channel:
        print(f"[scraper] ERRO: canal não encontrado na URL: {player_url}")
        return {"streams": []}
    channel = channel.group(1)

    print(f"[scraper] resolvendo canal={channel} fonte={fonte}")

    for attempt in range(3):
        print(f"[scraper] tentativa {attempt+1}/3 de obter token")
        token = asyncio.run(_scrape_token(player_url))
        if not token:
            print(f"[scraper] FALHA na tentativa {attempt+1}: sem token")
            return {"streams": []}
        try:
            r = _http.post(
                "https://api.cloudflaire.lat/get_token",
                headers={
                    "content-type": "application/json",
                    "Referer": f"https://{host}/",
                    "Origin": f"https://{host}",
                },
                json={"fonte": fonte, "channel": channel, "token": token},
                timeout=15,
            )
            print(f"[scraper] get_token status={r.status_code} (tentativa {attempt+1})")
            if r.status_code == 404:
                print("[scraper] token rejeitado pela API (404), tentando novo token...")
                continue
            if r.status_code != 200 and r.status_code != 201:
                print(f"[scraper] ERRO inesperado da API: status={r.status_code} body={r.text[:200]!r}")
                return {"streams": []}
            url = r.json().get("url")
            if url:
                print(f"[scraper] stream URL obtida: {url[:80]}...")
                return {"streams": [{"provider": "HD", "url": url, "referer": player_url}]}
            print(f"[scraper] ERRO: resposta sem campo 'url': {r.text[:200]!r}")
        except Exception as e:
            print(f"[scraper] ERRO na chamada get_token: {e}")

    print("[scraper] FALHA: todas as tentativas esgotadas")
    return {"streams": []}
