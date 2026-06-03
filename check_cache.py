#!/usr/bin/env python3
"""
check_cache.py — Testa URLs do stream_log.txt e adiciona as válidas ao cache.json

Uso:
  python check_cache.py              # testa e salva
  python check_cache.py --dry-run    # apenas testa, não salva
  python check_cache.py --workers 12 # mais threads (padrão: 8)
"""
import argparse, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR   = Path(__file__).parent
STREAM_LOG = BASE_DIR / "stream_log.txt"
CACHE_FILE = BASE_DIR / "cache.json"

sys.path.insert(0, str(BASE_DIR))
from scraper import check_stream, _channel_hash, _CACHE_TTL


# ── parsers ───────────────────────────────────────────────────────────────────

def parse_log() -> list[dict]:
    """Lê stream_log.txt, devolve entradas únicas por hash (sem duplicatas)."""
    entries = []
    seen: set[str] = set()
    text = STREAM_LOG.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = re.match(r'\[(.+?)\] (.+?) \| (.+?) \| referer: (.+)', line.strip())
        if not m:
            continue
        _, player_url, stream_url, referer = m.groups()
        h = _channel_hash(stream_url.strip())
        if not h or h in seen:
            continue
        seen.add(h)
        entries.append({
            "player_url": player_url.strip(),
            "stream_url": stream_url.strip(),
            "referer":    referer.strip(),
            "hash":       h,
        })
    return entries


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def cache_hashes(cache: dict) -> set[str]:
    hashes = set()
    for entries in cache.values():
        for _ts, entry in entries:
            h = _channel_hash(entry.get("url", ""))
            if h:
                hashes.add(h)
    return hashes


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Testa URLs do stream_log e adiciona válidas ao cache")
    ap.add_argument("--workers",  type=int, default=8,      help="Threads paralelas (padrão: 8)")
    ap.add_argument("--dry-run",  action="store_true",       help="Apenas testa, não salva")
    args = ap.parse_args()

    print(f"Lendo {STREAM_LOG.name}...")
    log_entries = parse_log()
    print(f"  {len(log_entries)} hashes únicos no log")

    cache = load_cache()
    existing = cache_hashes(cache)

    to_test = [e for e in log_entries if e["hash"] not in existing]
    print(f"  {len(log_entries) - len(to_test)} já no cache, {len(to_test)} para testar\n")

    if not to_test:
        print("Nada novo para testar.")
        return

    buckets: dict[str, list] = {"alive": [], "dead": [], "transient": []}

    def test_one(entry):
        return entry, check_stream(entry["stream_url"])

    print(f"Testando {len(to_test)} URLs com {args.workers} workers...\n")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(test_one, e): e for e in to_test}
        for i, fut in enumerate(as_completed(futs), 1):
            entry, status = fut.result()
            buckets[status].append(entry)
            icon = {"alive": "✓", "dead": "✗", "transient": "~"}[status]
            canal = entry["player_url"].rstrip("/").split("/")[-1]
            print(f"  [{i:3}/{len(to_test)}] {icon} {status:9}  hash={entry['hash'][:14]}  {canal}")

    alive = buckets["alive"]
    print(f"\nResultado: {len(alive)} vivas  |  {len(buckets['dead'])} mortas  |  {len(buckets['transient'])} transientes")

    if not alive:
        print("Nenhuma URL válida encontrada.")
        return

    if args.dry_run:
        print("\n--dry-run: cache não alterado.")
        return

    now = time.time()
    added = 0
    for entry in alive:
        player_url   = entry["player_url"]
        stream_entry = {"url": entry["stream_url"], "referer": entry["referer"]}
        # usa timestamp atual — URL está viva agora, TTL começa hoje
        cache.setdefault(player_url, []).append([now, stream_entry])
        added += 1
        canal = player_url.rstrip("/").split("/")[-1]
        print(f"  + {canal}  hash={entry['hash'][:14]}")

    save_cache(cache)
    print(f"\n{added} URL(s) adicionada(s) ao cache.json.")


if __name__ == "__main__":
    main()
