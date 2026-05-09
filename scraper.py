import os, base64, time
from playwright.sync_api import sync_playwright # type: ignore

DOMAIN = os.environ.get("SCRAPER_DOMAIN", "")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_AD_DOMAINS = [
    "doubleclick.net", "googlesyndication.com", "adservice.google",
    "amazon-adsystem.com", "adnxs.com", "outbrain.com", "taboola.com",
    "popads.net", "popcash.net", "trafficjunky.net", "exoclick.com",
]


def _launch(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=_UA,
        viewport={"width": 1280, "height": 800},
    )
    return browser, ctx


_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','pt','en-US','en']});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, 'permissions', {
  get: () => ({ query: (p) => Promise.resolve({state: p.name==='notifications'?'denied':'granted'}) })
});
"""

def _new_page(ctx):
    page = ctx.new_page()
    page.add_init_script(_STEALTH_SCRIPT)
    return page


def _block_ads(page):
    def handler(route):
        if any(d in route.request.url for d in _AD_DOMAINS):
            route.abort()
        else:
            route.continue_()
    page.route("**/*", handler)


def scrape_listings():
    if not DOMAIN:
        raise RuntimeError("SCRAPER_DOMAIN não configurado no .env")

    results = {}
    with sync_playwright() as p:
        browser, ctx = _launch(p)
        page = _new_page(ctx)
        _block_ads(page)
        try:
            page.goto(DOMAIN, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            page.evaluate("() => { const el = document.getElementById('dontfoid'); if (el) el.remove(); }")

            for el in page.query_selector_all(".fm-card a"):
                href = el.get_attribute("href") or ""
                title = el.query_selector(".fm-card-title, h3, h2, .title")
                name = (title.inner_text().strip() if title else el.inner_text().strip()).strip()
                if href and name:
                    results[name] = {"url": href, "type": "game"}

            for el in page.query_selector_all(".box-content-grid .item a"):
                href = el.get_attribute("href") or ""
                name = el.inner_text().strip()
                if href and name and name not in results:
                    results[name] = {"url": href, "type": "channel"}
        finally:
            browser.close()
    return results


def resolve_stream(page_url):
    streams = []
    with sync_playwright() as p:
        browser, ctx = _launch(p)
        try:
            page = _new_page(ctx)
            _block_ads(page)
            page.goto(page_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.evaluate("() => { const el = document.getElementById('dontfoid'); if (el) el.remove(); }")

            sources = []
            for btn in page.query_selector_all(".btn-player .btn-style"):
                src = btn.get_attribute("data-src") or ""
                label = btn.inner_text().strip() or f"Opção {len(sources) + 1}"
                if src:
                    sources.append({"label": label, "src": src})
            page.close()

            for source in sources[:4]:
                m3u8 = _find_m3u8(ctx, source["src"])
                if m3u8:
                    streams.append({
                        "provider": source["label"],
                        "url": m3u8,
                        "referer": source["src"],
                    })
        finally:
            browser.close()

    return {"streams": streams}


def _find_m3u8(ctx, src_url):
    found = []
    page = _new_page(ctx)

    def on_response(response):
        if found:
            return
        try:
            if response.status == 200:
                body = response.body()
                if body.lstrip()[:7] == b"#EXTM3U":
                    found.append(response.url)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(src_url, timeout=15000, wait_until="domcontentloaded")
        for _ in range(12):
            if found:
                break
            page.wait_for_timeout(500)
    except Exception:
        pass
    finally:
        page.close()

    return found[0] if found else None


# ── Debug live stream ─────────────────────────────────────────────────────────

def debug_live_frames(url, duration=60):
    """Yields JPEG screenshot bytes every 500 ms while browsing url."""
    with sync_playwright() as p:
        browser, ctx = _launch(p)
        page = _new_page(ctx)
        _block_ads(page)
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.evaluate("() => { const el = document.getElementById('dontfoid'); if (el) el.remove(); }")
            deadline = time.time() + duration
            while time.time() < deadline:
                yield page.screenshot(type="jpeg", quality=55, full_page=False)
                time.sleep(0.5)
        finally:
            browser.close()


def debug_live_resolve_frames(page_url, duration=90):
    """Yields JPEG frames while running the full resolve flow so you can watch each step."""
    with sync_playwright() as p:
        browser, ctx = _launch(p)
        try:
            # Step 1 – open game page
            page = _new_page(ctx)
            _block_ads(page)
            page.goto(page_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.evaluate("() => { const el = document.getElementById('dontfoid'); if (el) el.remove(); }")
            yield page.screenshot(type="jpeg", quality=55, full_page=False)

            sources = []
            for btn in page.query_selector_all(".btn-player .btn-style"):
                src = btn.get_attribute("data-src") or ""
                label = btn.inner_text().strip() or f"Opção {len(sources) + 1}"
                if src:
                    sources.append({"label": label, "src": src})
            page.close()

            # Step 2 – visit each source page
            deadline = time.time() + duration
            for source in sources[:4]:
                if time.time() >= deadline:
                    break
                found = []
                inner = _new_page(ctx)

                def on_response(r, _f=found):
                    if _f:
                        return
                    try:
                        if r.status == 200 and r.body().lstrip()[:7] == b"#EXTM3U":
                            _f.append(r.url)
                    except Exception:
                        pass

                inner.on("response", on_response)
                inner.goto(source["src"], timeout=15000, wait_until="domcontentloaded")

                for _ in range(24):
                    if found or time.time() >= deadline:
                        break
                    yield inner.screenshot(type="jpeg", quality=55, full_page=False)
                    time.sleep(0.5)

                inner.close()
        finally:
            browser.close()


# ── Debug helpers ─────────────────────────────────────────────────────────────

def debug_screenshot(url):
    """Abre a URL no browser, remove overlay de ad e retorna screenshot PNG como bytes."""
    with sync_playwright() as p:
        browser, ctx = _launch(p)
        page = _new_page(ctx)
        _block_ads(page)
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            page.evaluate("() => { const el = document.getElementById('dontfoid'); if (el) el.remove(); }")
            return page.screenshot(full_page=False)
        finally:
            browser.close()


def debug_resolve(page_url):
    """
    Versão verbosa do resolve_stream.
    Retorna um dict com:
    - sources: botões encontrados na página
    - per_source: para cada fonte, todas as respostas interceptadas e qual foi o m3u8
    - streams: resultado final
    """
    result = {"page_url": page_url, "sources": [], "per_source": [], "streams": []}

    with sync_playwright() as p:
        browser, ctx = _launch(p)
        try:
            page = _new_page(ctx)
            _block_ads(page)
            page.goto(page_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            page.evaluate("() => { const el = document.getElementById('dontfoid'); if (el) el.remove(); }")

            sources = []
            for btn in page.query_selector_all(".btn-player .btn-style"):
                src = btn.get_attribute("data-src") or ""
                label = btn.inner_text().strip() or f"Opção {len(sources) + 1}"
                if src:
                    sources.append({"label": label, "src": src})
            page.close()

            result["sources"] = sources

            for source in sources[:4]:
                intercepted = []
                found = []
                inner_page = _new_page(ctx)

                def on_response(response, _intercepted=intercepted, _found=found):
                    try:
                        entry = {
                            "url": response.url,
                            "status": response.status,
                            "content_type": response.headers.get("content-type", ""),
                            "is_m3u8": False,
                        }
                        if response.status == 200:
                            body = response.body()
                            if body.lstrip()[:7] == b"#EXTM3U":
                                entry["is_m3u8"] = True
                                if not _found:
                                    _found.append(response.url)
                        _intercepted.append(entry)
                    except Exception as e:
                        _intercepted.append({"url": response.url, "error": str(e)})

                inner_page.on("response", on_response)
                try:
                    inner_page.goto(source["src"], timeout=15000, wait_until="domcontentloaded")
                    for _ in range(12):
                        if found:
                            break
                        inner_page.wait_for_timeout(500)
                except Exception:
                    pass
                finally:
                    inner_page.close()

                m3u8 = found[0] if found else None
                result["per_source"].append({
                    "label": source["label"],
                    "src": source["src"],
                    "m3u8_found": m3u8,
                    "requests": intercepted,
                })
                if m3u8:
                    result["streams"].append({
                        "provider": source["label"],
                        "url": m3u8,
                        "referer": source["src"],
                    })
        finally:
            browser.close()

    return result


def debug_scrape():
    """Roda o scrape normal e também retorna screenshot da página inicial como base64."""
    if not DOMAIN:
        raise RuntimeError("SCRAPER_DOMAIN não configurado no .env")

    with sync_playwright() as p:
        browser, ctx = _launch(p)
        page = _new_page(ctx)
        _block_ads(page)
        listings = {}
        screenshot_b64 = None
        try:
            page.goto(DOMAIN, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            page.evaluate("() => { const el = document.getElementById('dontfoid'); if (el) el.remove(); }")

            screenshot_b64 = base64.b64encode(page.screenshot(full_page=False)).decode()

            for el in page.query_selector_all(".fm-card a"):
                href = el.get_attribute("href") or ""
                title = el.query_selector(".fm-card-title, h3, h2, .title")
                name = (title.inner_text().strip() if title else el.inner_text().strip()).strip()
                if href and name:
                    listings[name] = {"url": href, "type": "game"}

            for el in page.query_selector_all(".box-content-grid .item a"):
                href = el.get_attribute("href") or ""
                name = el.inner_text().strip()
                if href and name and name not in listings:
                    listings[name] = {"url": href, "type": "channel"}
        finally:
            browser.close()

    return {
        "domain": DOMAIN,
        "total": len(listings),
        "games": [k for k, v in listings.items() if v["type"] == "game"],
        "channels": [k for k, v in listings.items() if v["type"] == "channel"],
        "screenshot_base64": screenshot_b64,
    }
