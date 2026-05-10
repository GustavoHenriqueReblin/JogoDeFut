import os, time, re
import requests as _http

CAPSOLVER_KEY      = os.environ.get("CAPSOLVER_KEY", "")
_TURNSTILE_SITEKEY = os.environ.get("TURNSTILE_SITEKEY", "")

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


def _solve_turnstile(page_url):
    if not CAPSOLVER_KEY or not _TURNSTILE_SITEKEY:
        return None
    try:
        r = _http.post("https://api.capsolver.com/createTask", json={
            "clientKey": CAPSOLVER_KEY,
            "task": {
                "type": "AntiTurnstileTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": _TURNSTILE_SITEKEY,
            },
        }, timeout=30)
        task_id = r.json().get("taskId")
        if not task_id:
            return None
        for _ in range(30):
            time.sleep(2)
            res = _http.post("https://api.capsolver.com/getTaskResult", json={
                "clientKey": CAPSOLVER_KEY,
                "taskId": task_id,
            }, timeout=10).json()
            if res.get("status") == "ready":
                return res["solution"]["token"]
    except Exception:
        pass
    return None


def resolve_stream(player_url):
    host  = re.search(r"https?://([^/]+)", player_url)
    host  = host.group(1) if host else ""
    fonte = _CLOUDFLAIRE_PLAYERS.get(host)
    if not fonte:
        return {"streams": []}

    channel = re.search(r"/(?:tv/|embed/|)([^/?#]+)$", player_url)
    if not channel:
        return {"streams": []}
    channel = channel.group(1)

    token = _solve_turnstile(player_url)
    print(f"[scraper] turnstile token: {'ok' if token else 'FALHOU'}")
    if not token:
        return {"streams": []}

    try:
        print(f"[scraper] token obtido, chamando get_token para {fonte}/{channel}")
        r = _http.post("https://api.cloudflaire.lat/get_token",
            headers={
                "content-type": "application/json",
                "Referer": f"https://{host}/",
                "Origin":  f"https://{host}",
            },
            json={"fonte": fonte, "channel": channel, "token": token},
            timeout=15)
        print(f"[scraper] get_token status={r.status_code} body={r.text[:300]!r}")
        url = r.json().get("url")
        print(f"[scraper] get_token url: {url}")
        if url:
            body = _http.get(url, headers=_HEADERS, timeout=10).text
            print(f"[scraper] m3u8 body starts: {body[:80]!r}")
            if body.lstrip().startswith("#EXTM3U"):
                return {"streams": [{"provider": "HD", "url": url, "referer": player_url}]}
    except Exception as e:
        print(f"[scraper] erro get_token: {e}")

    return {"streams": []}
