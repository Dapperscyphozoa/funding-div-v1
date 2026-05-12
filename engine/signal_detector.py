"""
funding-div-v1 — Funding-rate divergence engine. v2 — historicised.

Behaviour:
  - PRODUCTION mode: reads current funding from HL metaAndAssetCtxs (cached)
  - BACKTEST mode: reads `funding` column from df, populated by the backtest
    candle-fetcher.

Detection:
  - df.attrs['mode'] == 'backtest' → expect df['funding'] column
  - else → fetch live funding (cached 10 min)

Bar-by-bar funding: HL returns hourly funding samples. For 1h candles the
mapping is 1:1. For 4h, we use the funding at the bar's open.

Signal: contrarian fade against extreme funding when price isn't following.
  SHORT: funding > funding_threshold_hi AND no new 8-bar high AND bearish close
  LONG:  funding < funding_threshold_lo AND no new 8-bar low AND bullish close
"""
from __future__ import annotations
import json
import time
import urllib.request
import numpy as np
import pandas as pd
from typing import Optional
from .config import STRATEGY_PARAMS, TRADE_PARAMS


_funding_cache = {"ts": 0, "data": {}}
_FUNDING_TTL = 600


def _fetch_funding_live_all() -> dict:
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


def _funding_for_bar(df: pd.DataFrame, coin: str) -> Optional[float]:
    """
    Return the funding rate to use for the latest bar.
      - If df has 'funding' column: backtest mode, use df['funding'].iloc[-1]
      - Else: live mode, fetch from HL
    """
    if "funding" in df.columns:
        val = df["funding"].iloc[-1]
        if pd.isna(val):
            return None
        return float(val)
    return _fetch_funding_live_all().get(coin)


def calc_atr(highs, lows, closes, period: int = 14) -> float:
    h_s = pd.Series(highs); l_s = pd.Series(lows); pc = pd.Series(closes).shift(1)
    tr = pd.concat([h_s - l_s, (h_s - pc).abs(), (l_s - pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _evaluate_with_thresholds(df: pd.DataFrame, funding_threshold_hi: float, funding_threshold_lo: float) -> Optional[dict]:
    F_HI = funding_threshold_hi
    F_LO = funding_threshold_lo
    coin = df.attrs.get("coin", "")
    if not coin: return None
    if df is None or len(df) < 30: return None

    fund = _funding_for_bar(df, coin)
    if fund is None:
        return None

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    last_c = float(closes[-1])

    is_long = None
    fire_reason = None
    if fund > F_HI:
        if last_c < float(np.max(highs[-9:-1])):
            if last_c < float(closes[-2]):
                is_long = False
                fire_reason = f"funding_hot_{fund*100:.5f}pct"
    elif fund < F_LO:
        if last_c > float(np.min(lows[-9:-1])):
            if last_c > float(closes[-2]):
                is_long = True
                fire_reason = f"funding_cold_{fund*100:.5f}pct"

    if is_long is None: return None

    atr = calc_atr(highs, lows, closes, TRADE_PARAMS["atr_period"])
    if not atr or atr <= 0: return None
    sl_m = TRADE_PARAMS["sl_atr_mult"]; tp_m = TRADE_PARAMS["tp_atr_mult"]
    if is_long:
        sl_p = last_c - sl_m * atr; tp_p = last_c + tp_m * atr
    else:
        sl_p = last_c + sl_m * atr; tp_p = last_c - tp_m * atr

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


def evaluate_latest_bar(df) -> Optional[dict]:
    """Tiered conviction scanner — strict (full size) + weak (quarter size)."""
    strict_hi = STRATEGY_PARAMS.get("funding_threshold_hi", 0.0001)
    strict_lo = STRATEGY_PARAMS.get("funding_threshold_lo", -0.0001)
    sig = _evaluate_with_thresholds(df, strict_hi, strict_lo)
    if sig is not None:
        sig["conviction"] = "strong"; sig["size_multiplier"] = 1.0
        sig["fire_reason"] = f"{sig.get('fire_reason','')}_STRONG"
        return sig
    weak_hi = STRATEGY_PARAMS.get("funding_threshold_hi_weak", 0.00005)
    weak_lo = STRATEGY_PARAMS.get("funding_threshold_lo_weak", -0.00005)
    sig = _evaluate_with_thresholds(df, weak_hi, weak_lo)
    if sig is not None:
        sig["conviction"] = "weak"; sig["size_multiplier"] = 0.25
        sig["fire_reason"] = f"{sig.get('fire_reason','')}_WEAK"
        return sig
    return None
