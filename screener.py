#!/usr/bin/env python3
"""
Solana Memecoin Screener
Finds high-momentum tokens via DexScreener and pushes alerts to your phone via ntfy.sh.

Usage:
    python screener.py          # run once
    python screener.py --loop   # run every 5 minutes
"""

import io
import sys
import time
import requests
from datetime import datetime, timezone

# Fix Windows console encoding so emoji/Unicode prints cleanly
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---- Config ------------------------------------------------------------------
NTFY_TOPIC  = "your-unique-topic-here"  # <-- change to something random & private
NTFY_SERVER = "https://ntfy.sh"

# Safety hard-filters — tokens outside these bounds are skipped
MIN_LIQ_USD   = 15_000      # $15k min liquidity  (below = likely rug)
MAX_LIQ_USD   = 3_000_000   # $3M  max liquidity  (above = too big to spike fast)
MIN_VOL_24H   = 75_000      # $75k min 24h volume
MAX_AGE_HOURS = 48          # pair must be newer than 48h

# Momentum — at least one must pass
MIN_PRICE_5M_PCT = 3.0      # +3%  in the last 5 minutes
MIN_PRICE_1H_PCT = 10.0     # +10% in the last 1 hour

# Core signal: money rushing in relative to pool size
MIN_VOL_LIQ_RATIO = 3.0     # 24h volume >= 3x liquidity

TOP_N = 5                   # coins per notification
LOOP_INTERVAL_SECS = 300    # 5 min between scans in --loop mode

# ---- DexScreener API (free, no key needed) -----------------------------------
DS_BASE      = "https://api.dexscreener.com"
DS_BOOSTS    = f"{DS_BASE}/token-boosts/top/v1"       # most boosted tokens
DS_PROFILES  = f"{DS_BASE}/token-profiles/latest/v1"  # freshly listed tokens
DS_TRENDING  = f"{DS_BASE}/metas/trending/v1"         # trending market categories
DS_TOKENS    = f"{DS_BASE}/latest/dex/tokens"         # pair data by token address


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


def fetch_candidate_addresses():
    """Pull Solana token addresses from boosted, profiled, and trending tokens."""
    addresses = set()

    # Top boosted (people paying to boost = social attention)
    data = _get(DS_BOOSTS)
    if isinstance(data, list):
        for item in data:
            if item.get("chainId") == "solana":
                addresses.add(item["tokenAddress"])

    # Freshly listed tokens with metadata
    data = _get(DS_PROFILES)
    if isinstance(data, list):
        for item in data:
            if item.get("chainId") == "solana":
                addresses.add(item["tokenAddress"])

    # Trending meta categories — extract token addresses embedded in each meta
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
      40 pts -- volume/liquidity ratio  (money rushing in)
      25 pts -- 1h price momentum
      15 pts -- 5m price momentum
      10 pts -- 6h price momentum
      10 pts -- buy-side pressure (buys / total txns in last hour)
    """
    liq   = (pair.get("liquidity") or {}).get("usd") or 0
    vol   = (pair.get("volume")    or {}).get("h24") or 0
    pc    = pair.get("priceChange") or {}
    p5m   = pc.get("m5") or 0
    p1h   = pc.get("h1") or 0
    p6h   = pc.get("h6") or 0
    h1_tx = (pair.get("txns") or {}).get("h1") or {}
    buys  = h1_tx.get("buys")  or 0
    sells = h1_tx.get("sells") or 0
    age   = _pair_age_hours(pair)

    # Hard filters
    if not (MIN_LIQ_USD <= liq <= MAX_LIQ_USD):
        return None
    if vol < MIN_VOL_24H:
        return None
    if age > MAX_AGE_HOURS:
        return None
    if liq == 0 or vol / liq < MIN_VOL_LIQ_RATIO:
        return None
    if p5m < MIN_PRICE_5M_PCT and p1h < MIN_PRICE_1H_PCT:
        return None

    # Composite score
    vol_liq_score = min(vol / liq, 50) / 50 * 40
    mom_1h_score  = min(max(p1h, 0), 100) / 100 * 25
    mom_5m_score  = min(max(p5m, 0), 50)  / 50  * 15
    mom_6h_score  = min(max(p6h, 0), 200) / 200 * 10
    buy_score     = (buys / (buys + sells) * 10) if (buys + sells) > 0 else 0

    return vol_liq_score + mom_1h_score + mom_5m_score + mom_6h_score + buy_score


def send_notification(ranked_coins):
    """Push top coins to phone via ntfy.sh."""
    lines = [f"Solana screener: {len(ranked_coins)} coin(s) detected\n"]

    for i, (score, pair) in enumerate(ranked_coins, 1):
        sym  = pair["baseToken"].get("symbol", "?")
        name = pair["baseToken"].get("name", "")
        liq  = (pair.get("liquidity") or {}).get("usd") or 0
        vol  = (pair.get("volume") or {}).get("h24") or 0
        pc   = pair.get("priceChange") or {}
        p5m  = pc.get("m5") or 0
        p1h  = pc.get("h1") or 0
        url  = pair.get("url", "")
        age  = _pair_age_hours(pair)

        lines.append(
            f"#{i} {sym} ({name})\n"
            f"   Score: {score:.0f}/100  Age: {age:.0f}h\n"
            f"   Liq: ${liq:,.0f}  Vol: ${vol:,.0f}\n"
            f"   5m: {p5m:+.1f}%  1h: {p1h:+.1f}%\n"
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

    if top:
        for rank, (score, pair) in enumerate(top, 1):
            sym = pair["baseToken"].get("symbol", "?")
            p1h = (pair.get("priceChange") or {}).get("h1") or 0
            vol = (pair.get("volume") or {}).get("h24") or 0
            print(f"  #{rank} {sym:<10}  1h:{p1h:+.1f}%  vol:${vol:,.0f}  score:{score:.0f}")
        send_notification(top)
    else:
        print("  No coins matched this scan. Market may be quiet or filters too strict.")


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
