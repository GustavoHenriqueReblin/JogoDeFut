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
# Sem TTL — entradas não expiram por idade (ver _valid_pool). Validade é
# decidida por teste real: is_stream_alive na inserção, is_stream_definitely_dead
# no warmup, eviction do relay em 404 confirmado.
_CACHE: dict[str, list[tuple[float, dict]]] = {}
_MIN_POOL_SIZE = int(os.environ.get("MIN_POOL_SIZE", 5))
_TOKEN_API_URL = os.environ["TOKEN_API_URL"]
_P2P_CDN_HOST = os.environ["P2P_CDN_HOST"]
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
    """Persiste tudo que está em _CACHE — sem filtro de TTL. A validade de uma
    entrada é decidida por teste real (is_stream_alive na inserção,
    is_stream_definitely_dead no warmup, eviction do relay em 404), não por
    idade. Uma URL sem tráfego por dias pode continuar perfeitamente viva."""
    try:
        with _CACHE_WRITE_LOCK:
            valid = {url: [[ts, entry] for ts, entry in pool] for url, pool in _CACHE.items() if pool}
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

def check_stream(stream_url: str) -> str:
    """
    Testa se uma URL de stream HLS está disponível.

    Retorna:
        'alive'     — M3U8 válido, live, P2P ok
        'dead'      — 404, stream encerrado ou P2P indisponível (erro permanente)
        'transient' — 5xx, timeout ou falha de rede (erro temporário)
    """
    try:
        r = _http.get(stream_url, timeout=10, allow_redirects=True)
        if r.status_code == 404:
            return "dead"
        if r.status_code >= 500:
            return "transient"   # CDN sobrecarregado — não confirma morte
        if r.status_code >= 400:
            return "dead"        # outro 4xx (403, 410…) = permanente
        body = r.text
        if not body.lstrip().startswith("#EXTM3U"):
            return "dead"
        if "#EXT-X-ENDLIST" in body:
            return "dead"        # stream encerrado
        p2p_png = next(
            (l.strip() for l in body.splitlines()
             if _P2P_CDN_HOST in l and l.strip().endswith(".png")),
            None,
        )
        if p2p_png:
            try:
                pr = _http.head(p2p_png, timeout=5, allow_redirects=True)
                if pr.status_code == 404:
                    return "dead"
            except Exception:
                pass  # erro de rede no check P2P → assume ok (evita falso positivo)
        return "alive"
    except Exception:
        return "transient"       # timeout, conexão recusada, DNS etc.


def is_stream_alive(stream_url: str) -> bool:
    """True se o stream está vivo. Usado na inserção — conservador: transiente = não adiciona."""
    return check_stream(stream_url) == "alive"


def is_stream_definitely_dead(stream_url: str, attempts: int = 3, delay: float = 5.0) -> bool:
    """
    True apenas se a URL estiver DEFINITIVAMENTE morta.
    Erros transientes (5xx/timeout) não contam como morte — só 'dead' confirma.
    Retorna False se qualquer tentativa retornar 'alive' ou se todas forem 'transient'.
    """
    for i in range(attempts):
        status = check_stream(stream_url)
        if status == "alive":
            return False
        if status == "dead":
            return True          # 404/encerrado: não precisa de 3 tentativas
        # 'transient': espera e tenta de novo
        if i < attempts - 1:
            time.sleep(delay)
    return False  # todas as tentativas foram transientes → não é definitivamente morto


# ── Pool helpers ──────────────────────────────────────────────────────────────

def _valid_pool(player_url: str) -> list[tuple[float, dict]]:
    """Retorna todo o pool do canal. Sem filtro de TTL — uma URL não expira só
    por idade, ela é removida quando prova estar morta de verdade (404
    confirmado, warmup validando via HEAD/GET). TTL por tempo é achismo: uma
    URL travada bloqueava a reinserção do mesmo hash quando 'expirava' mas
    ainda funcionava de verdade (ver bug reportado — dedup via hash ignorava
    o TTL e nunca re-adicionava, deixando o pool visivelmente vazio pra sempre)."""
    return list(_CACHE.get(player_url, []))


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

_MIN_TOKEN_LEN = 1200  # tokens aceitos pela API observados manualmente: ~1300-1450 chars


_DIRECT_URL_TIMEOUT = 15   # espera pela chamada get_token que o site faz sozinho
_FALLBACK_TOKEN_TIMEOUT = 15  # espera pelo token no DOM, só se o direct_url não vier


async def _simulate_human_interaction(page):
    """Gera sinais comportamentais (mouse/scroll). Só ajuda como último recurso
    no fallback — quando o Cloudflare já classificou a sessão como automatizada
    pelo fingerprint do ambiente, isso não muda o resultado (observado: token
    idêntico entre tentativas mesmo com interação)."""
    import random
    try:
        for _ in range(6):
            x, y = random.randint(50, 800), random.randint(50, 600)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.mouse.wheel(0, random.randint(100, 300))
        await asyncio.sleep(random.uniform(0.2, 0.4))
        await page.mouse.wheel(0, -random.randint(50, 150))
    except Exception as ex:
        _log(f"[scraper] erro ao simular interação: {ex}")


async def _scrape_token(page_url: str) -> tuple[str | None, str | None]:
    """Retorna (token, direct_url).

    direct_url: capturado interceptando a própria chamada get_token que o site
    faz sozinho dentro do browser — caminho confiável, evita replay externo do
    token (fingerprint TLS/HTTP de `requests` é diferente de um Chrome real e
    a API rejeita mesmo com token válido).

    token: fallback só usado se o site não fizer a chamada sozinho (permite ao
    chamador tentar o replay manual via requests, mesmo sabendo que pode falhar)."""
    token = None
    direct_url = None

    def _make_interceptors(page):
        async def intercept_response(response):
            nonlocal token, direct_url
            if "get_token" in response.url:
                try:
                    body = await response.text()
                    m = re.search(r'"url"\s*:\s*"([^"]+)"', body)
                    if m:
                        direct_url = m.group(1).replace("\\/", "/")
                        _log("[scraper] URL capturada direto da chamada get_token do próprio site")
                except Exception:
                    pass
                return
            if token or "challenges.cloudflare.com" not in response.url:
                return
            try:
                body = await response.text()
                m = re.search(r'"token"\s*:\s*"([^"]+)"', body)
                if m and m.group(1).count('.') >= 2 and len(m.group(1)) > _MIN_TOKEN_LEN:
                    token = m.group(1)
                    _log(f"[scraper] token capturado via response body len={len(token)}")
            except Exception:
                pass

        page.on("response", intercept_response)

    _log(f"[scraper] abrindo browser para: {page_url}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=_HEADLESS)
        ctx = await browser.new_context(locale="pt-BR", accept_downloads=False)
        page = await ctx.new_page()
        _make_interceptors(page)
        try:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()

            for _ in range(_DIRECT_URL_TIMEOUT):
                if direct_url:
                    break
                await asyncio.sleep(1)

            if not direct_url:
                _log("[scraper] site não resolveu sozinho, tentando fallback (interação + DOM)")
                await _simulate_human_interaction(page)
                for _ in range(_FALLBACK_TOKEN_TIMEOUT):
                    if direct_url or token:
                        break
                    t = await page.evaluate("""() => {
                        const el = document.querySelector('[name="cf-turnstile-response"]');
                        return el ? el.value : null;
                    }""")
                    if t and t.count('.') >= 2 and len(t) > _MIN_TOKEN_LEN:
                        token = t
                        _log(f"[scraper] token capturado via DOM len={len(t)}")
                        break
                    await asyncio.sleep(1)
        except Exception as e:
            _log(f"[scraper] erro ao carregar página: {e}")
        finally:
            await ctx.close()
            await browser.close()

    if direct_url:
        _log("[scraper] resolvido via URL direta")
    elif token:
        _log(f"[scraper] resolvido via token de fallback (len={len(token)}), qualidade incerta")
    else:
        _log("[scraper] FALHA: nem direct_url nem token válido capturados")
    return token, direct_url


def _get_lock(key: str) -> threading.Lock:
    with _LOCKS_META:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _channel_hash(stream_url: str) -> str | None:
    """Extrai o hash do canal da stream URL — formato MD5 (32 hex chars),
    independente do separador usado (ex: nossoplayer_{hash}/style.css ou
    nossoplayer/{hash}/file.txt). O host do CDN também é um MD5 sequencial
    (ver README), então pega o ÚLTIMO match — o hash do canal sempre vem
    depois do host no path, nunca antes."""
    matches = re.findall(r"[a-f0-9]{32}", stream_url)
    return matches[-1] if matches else None


def _accumulate_bg(player_url: str) -> bool:
    """Tenta acumular mais uma URL no pool (bloqueia o caller até terminar —
    quem quiser fire-and-forget deve rodar isso numa thread própria).
    Retorna True se o pool já estava completo ou ganhou/renovou uma entry viva."""
    lock = _get_lock(player_url)
    if not lock.acquire(blocking=True, timeout=90):
        return False
    try:
        current_pool = _valid_pool(player_url)
        if len(current_pool) >= _MIN_POOL_SIZE:
            return True
        new_entry = _do_resolve(player_url)
        if not new_entry:
            return False
        if not is_stream_alive(new_entry["url"]):
            _log(f"[scraper] URL resolvida não passou na validação (P2P/M3U8), descartando: {player_url}")
            return False
        now = time.time()
        new_hash = _channel_hash(new_entry["url"])
        pool = _CACHE.setdefault(player_url, [])
        existing_idx = next(
            (i for i, (_, e) in enumerate(pool) if _channel_hash(e.get("url", "")) == new_hash),
            None,
        ) if new_hash else None
        if existing_idx is None:
            pool.append((now, new_entry))
            _log(f"[scraper] hash novo adicionado ao pool (total: {pool_size(player_url)}): {player_url}")
        else:
            # mesmo hash já presente — atualiza timestamp/entry em vez de
            # ignorar (o resolve confirmou que ainda está vivo agora)
            pool[existing_idx] = (now, new_entry)
            _log(f"[scraper] hash já presente, timestamp atualizado: {player_url}")
        threading.Thread(target=_save_cache, daemon=True).start()
        threading.Thread(target=_append_stream_log, args=(player_url, new_entry), daemon=True).start()
        return True
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
        token, direct_url = asyncio.run(_scrape_token(player_url))
        if direct_url:
            _log(f"[scraper] usando URL capturada direto do browser (sem replay via requests): {direct_url}")
            return {"url": direct_url, "referer": player_url}
        if not token:
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            _log(f"[scraper] FALHA na tentativa {attempt+1}: sem token, aguardando {delay}s... ({attempt+1}/{_MAX_ATTEMPTS})")
            time.sleep(delay)
            continue
        try:
            _log(f"[scraper] POST get_token (tentativa {attempt+1})...")
            r = _http.post(
                _TOKEN_API_URL,
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
