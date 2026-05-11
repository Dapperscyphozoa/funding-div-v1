"""
backtester_with_funding.py — wraps backtester.fetch_hl_candles with optional
funding-rate join.

Use this in engines that need historical funding (funding-div-v1).

Behavior:
  1. Fetch candles via fetch_hl_candles
  2. Paginate HL fundingHistory across the full window (500-sample max per call)
  3. For each candle, attach the funding rate of the closest preceding sample
  4. Add 'funding' column to the DataFrame
  5. Caller's signal_detector reads df['funding'].iloc[-1]
"""
from __future__ import annotations
import json
import time
import urllib.request
import pandas as pd

from backtester import fetch_hl_candles


def fetch_hl_funding_full(coin: str, start_ms: int, end_ms: int) -> list:
    """Paginate fundingHistory — HL caps at 500 samples per request."""
    out = []
    cursor = start_ms
    while cursor < end_ms:
        body = json.dumps({"type": "fundingHistory", "coin": coin,
                           "startTime": cursor, "endTime": end_ms}).encode()
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                batch = json.loads(r.read())
        except Exception:
            break
        if not batch:
            break
        out.extend(batch)
        # Advance cursor past last sample
        last_t = int(batch[-1].get("time", 0))
        if last_t <= cursor:
            break   # no progress, bail
        cursor = last_t + 1
        # Throttle for HL rate limits
        time.sleep(0.3)
    return out


def fetch_hl_candles_with_funding(coin: str, days: int = 60,
                                     interval: str = "1h") -> pd.DataFrame:
    df = fetch_hl_candles(coin, days=days, interval=interval)
    if len(df) == 0:
        return df

    end_ms = int(df.index[-1].timestamp() * 1000) + 3_600_000
    start_ms = int(df.index[0].timestamp() * 1000) - 3_600_000
    funding_samples = fetch_hl_funding_full(coin, start_ms, end_ms)
    if not funding_samples:
        df["funding"] = float("nan")
        return df

    # Build funding series indexed by sample time
    f_df = pd.DataFrame(funding_samples)
    f_df["time"] = pd.to_datetime(f_df["time"], unit="ms", utc=True)
    f_df["fundingRate"] = f_df["fundingRate"].astype(float)
    f_df = f_df.set_index("time").sort_index()

    # asof-join — for each candle, get latest funding at-or-before bar time
    df = df.sort_index()
    df["funding"] = f_df["fundingRate"].reindex(df.index, method="ffill")
    df.attrs["coin"] = coin
    return df


if __name__ == "__main__":
    # quick smoke test
    import sys
    coin = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    df = fetch_hl_candles_with_funding(coin, days=days, interval="1h")
    print(f"{coin} {days}d: {len(df)} bars, {df['funding'].notna().sum()} with funding")
    print(df.tail(5)[["close", "funding"]])
    print(f"\nfunding stats: min={df['funding'].min()*100:.5f}% max={df['funding'].max()*100:.5f}% mean={df['funding'].mean()*100:.5f}%")
