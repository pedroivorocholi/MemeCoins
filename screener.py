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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

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
MIN_PRICE_1H_PCT = 3.0      # +3%  in the last 1 hour (lowered: catch coins before big 1h move)

# Core signal: money rushing in relative to pool size
MIN_VOL_LIQ_RATIO = 2.0     # 24h volume >= 2x liquidity

TOP_N = 5                   # coins per notification
LOOP_INTERVAL_SECS = 300    # 5 min between scans in --loop mode

# Early launch detection — separate pass for ultra-new pairs (< 45 min)
MAX_EARLY_AGE_MINS  = 45
MIN_EARLY_LIQ_USD   = 10_000
MIN_EARLY_5M_PCT    = 8.0    # was 15 — catch smaller initial moves
MIN_EARLY_BUYS      = 8      # was 10 — fewer txns needed for brand-new pairs
MIN_EARLY_BUY_RATIO = 0.60
MIN_EARLY_ACCEL     = 1.5    # was 2.0 — detect accumulation before volume explosion
TOP_EARLY_N         = 3      # was 2 — surface more early candidates

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

    Score breakdown (leading signals weighted higher to catch moves earlier):
      25 pts -- volume/liquidity ratio  (money rushing in vs pool size)
      25 pts -- 5m price momentum       (what is happening RIGHT NOW)
      20 pts -- buy transaction accel   (m5 buys vs h1 average — leads price)
      15 pts -- volume acceleration     (m5 vol vs h1 average)
      10 pts -- 1h price momentum       (context, not the primary signal)
       5 pts -- buy-side pressure (buys / total txns in last hour)
    """
    liq    = (pair.get("liquidity") or {}).get("usd") or 0
    vol24  = (pair.get("volume")    or {}).get("h24") or 0
    vol_h1 = (pair.get("volume")    or {}).get("h1")  or 0
    vol_m5 = (pair.get("volume")    or {}).get("m5")  or 0
    pc     = pair.get("priceChange") or {}
    p5m    = pc.get("m5") or 0
    p1h    = pc.get("h1") or 0
    p6h    = pc.get("h6") or 0
    p24h   = pc.get("h24") or 0
    h1_tx  = (pair.get("txns") or {}).get("h1") or {}
    m5_tx  = (pair.get("txns") or {}).get("m5") or {}
    buys   = h1_tx.get("buys")  or 0
    sells  = h1_tx.get("sells") or 0
    m5_buys = m5_tx.get("buys") or 0
    age    = _pair_age_hours(pair)
    buy_ratio = buys / (buys + sells) if (buys + sells) > 0 else 0

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
    # Dead cat bounce: skip coins already down big over 24h
    if p24h < -30:
        return None
    # Require buyers to outnumber sellers
    if buy_ratio < 0.55:
        return None
    # Require meaningful transaction activity (filters wash trading / thin markets)
    if buys < 30:
        return None
    # Momentum freshness: current 5m pace must be at least half the implied hourly pace.
    # Catches moves that already peaked — coin up 40% in 1h but barely moving now.
    if p1h > 0 and p5m < (p1h / 12) * 0.5:
        return None

    # Vol/liq ratio score
    vol_liq_score = min(vol24 / liq, 50) / 50 * 25

    # 5m momentum (primary leading signal)
    mom_5m_score = min(max(p5m, 0), 50) / 50 * 25

    # Buy transaction acceleration: m5 buys vs average 5-min slice of h1.
    # This leads price — accumulation happens before the candle prints green.
    avg_m5_buys  = buys / 12 if buys > 0 else 0
    buy_accel    = (m5_buys / avg_m5_buys) if avg_m5_buys > 0 else 1.0
    buy_accel_score = min(buy_accel, 5) / 5 * 20

    # Volume acceleration (m5 vol vs h1 average)
    avg_5m_vol  = vol_h1 / 12 if vol_h1 > 0 else 0
    vol_accel   = (vol_m5 / avg_5m_vol) if avg_5m_vol > 0 else 1.0
    accel_score = min(vol_accel, 5) / 5 * 15

    # 1h momentum (context signal, not the primary driver)
    mom_1h_score = min(max(p1h, 0), 100) / 100 * 10

    # Buy pressure (1h)
    buy_score = (buy_ratio * 5) if (buys + sells) > 0 else 0

    return vol_liq_score + mom_5m_score + buy_accel_score + accel_score + mom_1h_score + buy_score


# ---- Recommendation ----------------------------------------------------------

def recommendation_score(pair, score):
    """
    Returns a 0-100 confidence score for a strong entry recommendation,
    or 0 if the coin doesn't meet the stricter multi-signal criteria.

    Requires ALL of:
      - Base score >= 60
      - 1h momentum >= 15%  (sustained move, not a flash)
      - 5m momentum >= 3%   (still moving right now)
      - Buy ratio  >= 60%   (buyers outnumber sellers)
      - Age 1-18h           (young enough to have upside, old enough to trust)
      - Vol/Liq   >= 4x     (strong relative money flow)

    Confidence then grades how well each criterion is exceeded.
    """
    liq    = (pair.get("liquidity") or {}).get("usd") or 0
    vol24  = (pair.get("volume")    or {}).get("h24") or 0
    vol_h1 = (pair.get("volume")    or {}).get("h1")  or 0
    vol_m5 = (pair.get("volume")    or {}).get("m5")  or 0
    pc     = pair.get("priceChange") or {}
    p5m    = pc.get("m5") or 0
    p1h    = pc.get("h1") or 0
    h1_tx  = (pair.get("txns") or {}).get("h1") or {}
    buys   = h1_tx.get("buys")  or 0
    sells  = h1_tx.get("sells") or 0
    age    = _pair_age_hours(pair)
    buy_ratio  = buys / (buys + sells) if (buys + sells) > 0 else 0
    vol_liq    = vol24 / liq if liq > 0 else 0

    m5_tx   = (pair.get("txns") or {}).get("m5") or {}
    m5_buys = m5_tx.get("buys") or 0
    avg_m5_buys = buys / 12 if buys > 0 else 0
    buy_accel   = m5_buys / avg_m5_buys if avg_m5_buys > 0 else 1.0

    # All gates must pass
    if score < 60:              return 0
    if p1h < 5:                 return 0   # was 15 — catch coins before big 1h move
    if p5m < 3:                 return 0
    if buy_ratio < 0.60:        return 0
    if not (0.5 <= age <= 18):  return 0   # was 2h min — allow 30-min-old coins
    if vol_liq < 4:             return 0
    if buy_accel < 1.5:         return 0   # must show accelerating buys right now

    # Grade how far each signal exceeds its minimum
    conf  = min(score, 100) / 100 * 20             # base score (20 pts)
    conf += min(p5m, 30) / 30 * 25                 # 5m momentum up to 30% (25 pts)
    conf += min(buy_accel, 5) / 5 * 25             # buy accel up to 5x    (25 pts)
    conf += (buy_ratio - 0.60) / 0.40 * 15         # buy ratio 60-100%     (15 pts)
    avg_5m = vol_h1 / 12 if vol_h1 > 0 else 0
    accel  = vol_m5 / avg_5m if avg_5m > 0 else 1
    conf += min(accel, 5) / 5 * 15                 # vol acceleration up to 5x (15 pts)

    return min(conf, 100)


# ---- Early launch detection --------------------------------------------------

def early_launch_score(pair):
    """
    Separate scorer for ultra-new pairs (< 45 min old).
    Ignores 24h metrics since the coin hasn't existed long enough to build them.
    Scores on immediate momentum: 5m move, buy transaction acceleration, buy pressure,
    and volume acceleration. Buy accel (txns.m5.buys vs h1 average) is the primary
    leading signal — it shows accumulation before price has printed a big move.
    Returns 0-100 or None if filters not met.
    """
    liq     = (pair.get("liquidity") or {}).get("usd") or 0
    vol_h1  = (pair.get("volume") or {}).get("h1") or 0
    vol_m5  = (pair.get("volume") or {}).get("m5") or 0
    pc      = pair.get("priceChange") or {}
    p5m     = pc.get("m5") or 0
    h1_tx   = (pair.get("txns") or {}).get("h1") or {}
    m5_tx   = (pair.get("txns") or {}).get("m5") or {}
    buys    = h1_tx.get("buys") or 0
    sells   = h1_tx.get("sells") or 0
    m5_buys = m5_tx.get("buys") or 0
    age     = _pair_age_hours(pair)
    buy_ratio  = buys / (buys + sells) if (buys + sells) > 0 else 0
    avg_5m_vol = vol_h1 / 12 if vol_h1 > 0 else 0
    vol_accel  = vol_m5 / avg_5m_vol if avg_5m_vol > 0 else 0
    avg_m5_buys = buys / 12 if buys > 0 else 0
    buy_accel   = m5_buys / avg_m5_buys if avg_m5_buys > 0 else 0

    if age > MAX_EARLY_AGE_MINS / 60:   return None
    if liq < MIN_EARLY_LIQ_USD:         return None
    if p5m < MIN_EARLY_5M_PCT:          return None
    if buys < MIN_EARLY_BUYS:           return None
    if buy_ratio < MIN_EARLY_BUY_RATIO: return None
    if vol_accel < MIN_EARLY_ACCEL:     return None

    mom_score        = min(p5m, 100) / 100 * 30
    buy_accel_score  = min(buy_accel, 8) / 8 * 40  # primary leading signal
    buy_ratio_score  = (buy_ratio - 0.60) / 0.40 * 15
    vol_accel_score  = min(vol_accel, 8) / 8 * 15
    return mom_score + buy_accel_score + buy_ratio_score + vol_accel_score


# ---- News search -------------------------------------------------------------

def fetch_news(name, symbol, max_age_hours=4):
    """Search Google News RSS for recent articles about the token."""
    query = quote_plus(f'"{name}" crypto OR "{symbol}" solana')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        headlines = []
        for item in root.findall(".//item")[:8]:
            title = (item.findtext("title") or "").strip()
            pub_str = item.findtext("pubDate") or ""
            try:
                pub_dt = parsedate_to_datetime(pub_str)
                if pub_dt >= cutoff and title:
                    headlines.append(title)
            except Exception:
                pass
        return headlines[:2]
    except Exception as e:
        print(f"  [news] {name}: {e}")
        return []


# ---- Notification ------------------------------------------------------------

def _pair_summary(score, pair, rank=None, conf=None, news=None, early=False):
    sym     = pair["baseToken"].get("symbol", "?")
    name    = pair["baseToken"].get("name", "")
    liq     = (pair.get("liquidity") or {}).get("usd") or 0
    vol     = (pair.get("volume") or {}).get("h24") or 0
    vol_h1  = (pair.get("volume") or {}).get("h1") or 0
    vol_m5  = (pair.get("volume") or {}).get("m5") or 0
    pc      = pair.get("priceChange") or {}
    p5m     = pc.get("m5") or 0
    p1h     = pc.get("h1") or 0
    h1_tx   = (pair.get("txns") or {}).get("h1") or {}
    m5_tx   = (pair.get("txns") or {}).get("m5") or {}
    h1_buys = h1_tx.get("buys") or 0
    m5_buys = m5_tx.get("buys") or 0
    url     = pair.get("url", "")
    age     = _pair_age_hours(pair)
    avg_5m_vol  = vol_h1 / 12 if vol_h1 > 0 else 0
    vol_accel   = vol_m5 / avg_5m_vol if avg_5m_vol > 0 else 0
    avg_m5_buys = h1_buys / 12 if h1_buys > 0 else 0
    buy_accel   = m5_buys / avg_m5_buys if avg_m5_buys > 0 else 0
    accel_tag   = f"  ACCEL vol:{vol_accel:.1f}x buys:{buy_accel:.1f}x\n" if vol_accel >= 1.5 or buy_accel >= 1.5 else ""
    prefix      = f"#{rank} " if rank else ""
    conf_tag    = f"  Confidence: {conf:.0f}/100\n" if conf is not None else ""
    early_tag   = "  EARLY LAUNCH\n" if early else ""
    news_lines  = "".join(f"  NEWS: {h}\n" for h in (news or []))
    age_str     = f"{age * 60:.0f}min" if early else f"{age:.1f}h"
    return (
        f"{prefix}{sym} ({name})\n"
        f"   Score: {score:.0f}/100  Age: {age_str}\n"
        f"{early_tag}"
        f"{conf_tag}"
        f"   Liq: ${liq:,.0f}  24h Vol: ${vol:,.0f}\n"
        f"   5m: {p5m:+.1f}%  1h: {p1h:+.1f}%\n"
        f"{accel_tag}"
        f"{news_lines}"
        f"   {url}\n"
    )


def send_notification(ranked_coins, early_coins=None):
    """Push top coins to phone via ntfy.sh."""
    early_coins = early_coins or []

    # Compute recommendation confidence for regular coins
    picks = [
        (recommendation_score(pair, score), score, pair)
        for score, pair in ranked_coins
    ]
    picks.sort(key=lambda x: x[0], reverse=True)
    recommended = [(conf, score, pair) for conf, score, pair in picks if conf > 0][:2]

    total = len(ranked_coins) + len(early_coins)
    lines = [f"Solana screener: {total} coin(s) detected\n"]

    if early_coins:
        lines.append("*** EARLY LAUNCHES ***")
        for score, pair in early_coins:
            sym  = pair["baseToken"].get("symbol", "?")
            name = pair["baseToken"].get("name", "")
            news = fetch_news(name, sym)
            lines.append(_pair_summary(score, pair, early=True, news=news))

    if recommended:
        lines.append("★ RECOMMENDED ENTRY ★")
        for conf, score, pair in recommended:
            sym  = pair["baseToken"].get("symbol", "?")
            name = pair["baseToken"].get("name", "")
            news = fetch_news(name, sym)
            lines.append(_pair_summary(score, pair, conf=conf, news=news))
        lines.append("--- All signals ---")

    for i, (score, pair) in enumerate(ranked_coins, 1):
        lines.append(_pair_summary(score, pair, rank=i))

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
    early_scored = []
    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue
        es = early_launch_score(pair)
        if es is not None:
            early_scored.append((es, pair))
            continue  # don't double-count in regular scorer
        s = score_pair(pair)
        if s is not None:
            scored.append((s, pair))

    scored.sort(key=lambda x: x[0], reverse=True)
    early_scored.sort(key=lambda x: x[0], reverse=True)

    scored = [x for x in scored if x[0] >= 60]
    top       = scored[:TOP_N]
    top_early = early_scored[:TOP_EARLY_N]

    print(f"  {len(scored)} passed filters -> top {len(top)} selected")
    print(f"  {len(early_scored)} early launches -> top {len(top_early)} selected")

    if not top and not top_early:
        print("  No coins matched this scan. Market may be quiet or filters too strict.")
        return

    for rank, (score, pair) in enumerate(top, 1):
        sym  = pair["baseToken"].get("symbol", "?")
        p1h  = (pair.get("priceChange") or {}).get("h1") or 0
        vol  = (pair.get("volume") or {}).get("h24") or 0
        age  = _pair_age_hours(pair)
        print(f"  #{rank} {sym:<10}  age:{age:.0f}h  1h:{p1h:+.1f}%  vol:${vol:,.0f}  score:{score:.0f}")

    for score, pair in top_early:
        sym  = pair["baseToken"].get("symbol", "?")
        p5m  = (pair.get("priceChange") or {}).get("m5") or 0
        age  = _pair_age_hours(pair)
        print(f"  EARLY {sym:<10}  age:{age*60:.0f}min  5m:{p5m:+.1f}%  score:{score:.0f}")

    # Dedup — only alert on coins not seen recently
    seen = load_seen()
    top_new = [
        (s, p) for s, p in top
        if not is_recently_seen(seen, (p.get("baseToken") or {}).get("address", ""))
    ]
    top_early_new = [
        (s, p) for s, p in top_early
        if not is_recently_seen(seen, (p.get("baseToken") or {}).get("address", ""))
    ]

    if top_new or top_early_new:
        send_notification(top_new, early_coins=top_early_new)
        for _, p in top_new + top_early_new:
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
