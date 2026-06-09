#!/usr/bin/env python3
"""
Solana Memecoin Screener
Finds high-momentum tokens via DexScreener and pushes alerts to your phone via ntfy.sh.

Usage:
    python screener.py          # run once
    python screener.py --loop   # run every 5 minutes

Config via env vars (for GitHub Actions):
    NTFY_TOPIC   — your ntfy.sh topic
    NTFY_SERVER  — defaults to https://ntfy.sh
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Fix Windows console encoding so emoji/Unicode prints cleanly
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---- Config ------------------------------------------------------------------
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "your-unique-topic-here")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# Safety hard-filters — tokens outside these bounds are skipped
MIN_LIQ_USD   = 15_000      # $15k min liquidity  (below = likely rug)
MAX_LIQ_USD   = 3_000_000   # $3M  max liquidity  (above = too big to spike fast)
MIN_VOL_24H   = 50_000      # $50k min 24h volume
MAX_AGE_HOURS = 48          # pair must be newer than 48h

# Momentum — at least one must pass
MIN_PRICE_5M_PCT = 2.0      # +2%  in the last 5 minutes
MIN_PRICE_1H_PCT = 8.0      # +8%  in the last 1 hour

# Core signal: money rushing in relative to pool size
MIN_VOL_LIQ_RATIO = 2.0     # 24h volume >= 2x liquidity

TOP_N = 5                   # coins per notification
LOOP_INTERVAL_SECS = 300    # 5 min between scans in --loop mode

# Dedup — skip coins already alerted within this window
SEEN_CACHE_FILE = Path("cache/seen.json")
SEEN_TTL_HOURS  = 4

# ---- DexScreener API (free, no key needed) -----------------------------------
DS_BASE      = "https://api.dexscreener.com"
DS_BOOSTS    = f"{DS_BASE}/token-boosts/top/v1"
DS_PROFILES  = f"{DS_BASE}/token-profiles/latest/v1"
DS_TRENDING  = f"{DS_BASE}/metas/trending/v1"
DS_TOKENS    = f"{DS_BASE}/latest/dex/tokens"


def _get(url, params=None):
    try:
        r = requests.get(
            url, params=params, timeout=12,
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] {url} -> {e}")
        return None


# ---- Seen-token dedup --------------------------------------------------------

def load_seen():
    if SEEN_CACHE_FILE.exists():
        try:
            return json.loads(SEEN_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_seen(seen):
    SEEN_CACHE_FILE.parent.mkdir(exist_ok=True)
    SEEN_CACHE_FILE.write_text(json.dumps(seen))


def is_recently_seen(seen, address):
    if not address or address not in seen:
        return False
    age_hours = (time.time() - seen[address]) / 3600
    return age_hours < SEEN_TTL_HOURS


def mark_seen(seen, address):
    seen[address] = time.time()
    cutoff = time.time() - SEEN_TTL_HOURS * 3600
    return {k: v for k, v in seen.items() if v > cutoff}


# ---- Candidate fetching ------------------------------------------------------

def fetch_candidate_addresses():
    """Pull Solana token addresses from boosted, profiled, and trending tokens."""
    addresses = set()

    data = _get(DS_BOOSTS)
    if isinstance(data, list):
        for item in data:
            if item.get("chainId") == "solana":
                addresses.add(item["tokenAddress"])

    data = _get(DS_PROFILES)
    if isinstance(data, list):
        for item in data:
            if item.get("chainId") == "solana":
                addresses.add(item["tokenAddress"])

    data = _get(DS_TRENDING)
    if isinstance(data, list):
        for meta in data:
            for token in meta.get("tokens") or []:
                if token.get("chainId") == "solana":
                    addresses.add(token["address"])

    return list(addresses)


def fetch_pairs(addresses):
    """Batch-fetch DexScreener pair data (30 addresses per request)."""
    pairs = []
    for i in range(0, len(addresses), 30):
        batch = addresses[i : i + 30]
        data = _get(f"{DS_TOKENS}/{','.join(batch)}")
        if data:
            pairs.extend(data.get("pairs") or [])
        time.sleep(0.25)
    return pairs


# ---- Scoring -----------------------------------------------------------------

def _pair_age_hours(pair):
    created = pair.get("pairCreatedAt")
    if not created:
        return 9999
    ts = created / 1000 if created > 1e10 else created
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    return delta.total_seconds() / 3600


def score_pair(pair):
    """
    Returns a 0-100 composite score, or None if the pair fails hard filters.

    Score breakdown:
      35 pts -- volume/liquidity ratio  (money rushing in vs pool size)
      20 pts -- 1h price momentum
      15 pts -- 5m price momentum
      10 pts -- 6h price momentum
      10 pts -- buy-side pressure (buys / total txns in last hour)
      10 pts -- volume acceleration (m5 volume vs hourly average)
    """
    liq    = (pair.get("liquidity") or {}).get("usd") or 0
    vol24  = (pair.get("volume")    or {}).get("h24") or 0
    vol_h1 = (pair.get("volume")    or {}).get("h1")  or 0
    vol_m5 = (pair.get("volume")    or {}).get("m5")  or 0
    pc     = pair.get("priceChange") or {}
    p5m    = pc.get("m5") or 0
    p1h    = pc.get("h1") or 0
    p6h    = pc.get("h6") or 0
    h1_tx  = (pair.get("txns") or {}).get("h1") or {}
    buys   = h1_tx.get("buys")  or 0
    sells  = h1_tx.get("sells") or 0
    age    = _pair_age_hours(pair)

    # Hard filters
    if not (MIN_LIQ_USD <= liq <= MAX_LIQ_USD):
        return None
    if vol24 < MIN_VOL_24H:
        return None
    if age > MAX_AGE_HOURS:
        return None
    if liq == 0 or vol24 / liq < MIN_VOL_LIQ_RATIO:
        return None
    if p5m < MIN_PRICE_5M_PCT and p1h < MIN_PRICE_1H_PCT:
        return None

    # Vol/liq ratio score
    vol_liq_score = min(vol24 / liq, 50) / 50 * 35

    # Momentum scores
    mom_1h_score  = min(max(p1h, 0), 100) / 100 * 20
    mom_5m_score  = min(max(p5m, 0), 50)  / 50  * 15
    mom_6h_score  = min(max(p6h, 0), 200) / 200 * 10

    # Buy pressure (1h)
    buy_score = (buys / (buys + sells) * 10) if (buys + sells) > 0 else 0

    # Volume acceleration: compare last 5m to the avg 5-min slice of the last hour.
    # A ratio > 2 means volume is surging in the last 5 minutes — early spike signal.
    avg_5m_vol = vol_h1 / 12 if vol_h1 > 0 else 0
    accel_ratio = (vol_m5 / avg_5m_vol) if avg_5m_vol > 0 else 1.0
    accel_score = min(accel_ratio, 5) / 5 * 10  # caps at 5x acceleration

    return vol_liq_score + mom_1h_score + mom_5m_score + mom_6h_score + buy_score + accel_score


# ---- Notification ------------------------------------------------------------

def send_notification(ranked_coins):
    """Push top coins to phone via ntfy.sh."""
    lines = [f"Solana screener: {len(ranked_coins)} coin(s) detected\n"]

    for i, (score, pair) in enumerate(ranked_coins, 1):
        sym    = pair["baseToken"].get("symbol", "?")
        name   = pair["baseToken"].get("name", "")
        liq    = (pair.get("liquidity") or {}).get("usd") or 0
        vol    = (pair.get("volume") or {}).get("h24") or 0
        vol_m5 = (pair.get("volume") or {}).get("m5") or 0
        pc     = pair.get("priceChange") or {}
        p5m    = pc.get("m5") or 0
        p1h    = pc.get("h1") or 0
        url    = pair.get("url", "")
        age    = _pair_age_hours(pair)

        # Volume acceleration label
        vol_h1 = (pair.get("volume") or {}).get("h1") or 0
        avg_5m = vol_h1 / 12 if vol_h1 > 0 else 0
        accel  = vol_m5 / avg_5m if avg_5m > 0 else 0
        accel_tag = f"  ACCEL {accel:.1f}x\n" if accel >= 2 else ""

        lines.append(
            f"#{i} {sym} ({name})\n"
            f"   Score: {score:.0f}/100  Age: {age:.0f}h\n"
            f"   Liq: ${liq:,.0f}  24h Vol: ${vol:,.0f}\n"
            f"   5m: {p5m:+.1f}%  1h: {p1h:+.1f}%\n"
            f"{accel_tag}"
            f"   {url}\n"
        )

    message = "\n".join(lines)
    try:
        resp = requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": "Memecoin Alert",
                "Priority": "high",
                "Tags": "moneybag,chart_with_upwards_trend",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            print("  [ntfy] notification sent")
        else:
            print(f"  [ntfy] unexpected status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"  [ntfy] failed: {e}")


# ---- Main scan ---------------------------------------------------------------

def run_once():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning Solana memecoins...")

    print("  Fetching candidates from DexScreener...")
    addresses = fetch_candidate_addresses()
    print(f"  {len(addresses)} candidates found")

    if not addresses:
        print("  No candidates -- DexScreener may be rate-limiting. Try again shortly.")
        return

    print("  Fetching pair data...")
    pairs = fetch_pairs(addresses)
    print(f"  {len(pairs)} pairs retrieved")

    scored = []
    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue
        s = score_pair(pair)
        if s is not None:
            scored.append((s, pair))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:TOP_N]

    print(f"  {len(scored)} passed filters -> top {len(top)} selected")

    if not top:
        print("  No coins matched this scan. Market may be quiet or filters too strict.")
        return

    for rank, (score, pair) in enumerate(top, 1):
        sym  = pair["baseToken"].get("symbol", "?")
        p1h  = (pair.get("priceChange") or {}).get("h1") or 0
        vol  = (pair.get("volume") or {}).get("h24") or 0
        age  = _pair_age_hours(pair)
        print(f"  #{rank} {sym:<10}  age:{age:.0f}h  1h:{p1h:+.1f}%  vol:${vol:,.0f}  score:{score:.0f}")

    # Dedup — only alert on coins not seen recently
    seen = load_seen()
    top_new = [
        (s, p) for s, p in top
        if not is_recently_seen(seen, (p.get("baseToken") or {}).get("address", ""))
    ]

    if top_new:
        send_notification(top_new)
        for _, p in top_new:
            addr = (p.get("baseToken") or {}).get("address", "")
            if addr:
                seen = mark_seen(seen, addr)
        save_seen(seen)
    else:
        print("  All top coins already alerted recently — skipping notification.")


def main():
    loop_mode = "--loop" in sys.argv

    if loop_mode:
        print(f"Loop mode -- scanning every {LOOP_INTERVAL_SECS // 60} minutes. Ctrl+C to stop.")
        while True:
            try:
                run_once()
                print(f"  Next scan in {LOOP_INTERVAL_SECS // 60} minutes...")
                time.sleep(LOOP_INTERVAL_SECS)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
    else:
        run_once()


if __name__ == "__main__":
    main()
