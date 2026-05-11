"""
funding-div-v1 — Funding-rate divergence engine.
Shorts crowded-long perps (high funding, no new high). Longs the inverse.
Reads funding via HL metaAndAssetCtxs in scheduler (cached); detector reads
last value via the bar's `funding` column injected by data layer.
For now we approximate: detector checks 4H momentum + 1H structure + uses
config-driven funding thresholds. The scheduler will inject funding into
extras via the universe-level fetch — strategy reads df.attrs.get("funding").
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import json, urllib.request, time
from typing import Optional
from .config import STRATEGY_PARAMS, TRADE_PARAMS


_funding_cache = {"ts": 0, "data": {}}
_FUNDING_TTL = 600  # 10 min


def _fetch_funding_all() -> dict:
    """Cached fetch of all coins' funding (hr rate)."""
    now = time.time()
    if now - _funding_cache["ts"] < _FUNDING_TTL and _funding_cache["data"]:
        return _funding_cache["data"]
    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            mc = json.loads(r.read())
        out = {}
        for u, c in zip(mc[0]["universe"], mc[1]):
            try: out[u["name"]] = float(c.get("funding", 0))
            except: pass
        _funding_cache["ts"] = now
        _funding_cache["data"] = out
        return out
    except Exception:
        return _funding_cache["data"]


def calc_atr(highs, lows, closes, period: int = 14) -> float:
    h_s = pd.Series(highs); l_s = pd.Series(lows); pc = pd.Series(closes).shift(1)
    tr = pd.concat([h_s - l_s, (h_s - pc).abs(), (l_s - pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def evaluate_latest_bar(df: pd.DataFrame) -> Optional[dict]:
    F_HI = STRATEGY_PARAMS.get("funding_threshold_hi", 0.0003)   # 0.03% hourly
    F_LO = STRATEGY_PARAMS.get("funding_threshold_lo", -0.0002)
    coin = df.attrs.get("coin", "")
    if not coin: return None
    if df is None or len(df) < 30: return None

    fund = _fetch_funding_all().get(coin, 0.0)
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    last_c = float(closes[-1])

    is_long = None
    fire_reason = None
    # SHORT: funding too high → longs over-paying → fade
    if fund > F_HI:
        # No new 8-bar high
        if last_c < float(np.max(highs[-9:-1])):
            # Bearish bar
            if last_c < float(closes[-2]):
                is_long = False
                fire_reason = f"funding_hot_{fund*100:.4f}pct"
    elif fund < F_LO:
        if last_c > float(np.min(lows[-9:-1])):
            if last_c > float(closes[-2]):
                is_long = True
                fire_reason = f"funding_cold_{fund*100:.4f}pct"

    if is_long is None: return None

    atr = calc_atr(highs, lows, closes, TRADE_PARAMS["atr_period"])
    if not atr or atr <= 0: return None
    sl_mult = TRADE_PARAMS["sl_atr_mult"]
    tp_mult = TRADE_PARAMS["tp_atr_mult"]
    if is_long:
        sl_p = last_c - sl_mult * atr; tp_p = last_c + tp_mult * atr
    else:
        sl_p = last_c + sl_mult * atr; tp_p = last_c - tp_mult * atr

    return {
        "fire_ts": df.index[-1], "ref_price": last_c, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_p), "tp_px": float(tp_p),
        "max_hold_bars": TRADE_PARAMS["max_hold_bars"],
        "fire_reason": fire_reason,
        "raw_direction": "LONG" if is_long else "SHORT",
        "fade_direction": "LONG" if is_long else "SHORT",
        "funding_rate": float(fund),
    }
