"""funding-div-v1 backtest — uses HL fundingHistory for historical accuracy."""
import os, sys, time
os.environ.setdefault("ENGINE_NAME", "funding-div-v1")
os.environ.setdefault("STATE_DIR", "/tmp/backtest-state")

from backtester_with_funding import fetch_hl_candles_with_funding
from backtester import run_backtest, sweep_results
from engine.config import ACTIVE_UNIVERSE, TRADE_PARAMS, ENGINE_NAME
from engine.signal_detector import evaluate_latest_bar

days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
coins = sys.argv[2].split(",") if len(sys.argv) > 2 else ACTIVE_UNIVERSE
print(f"=== {ENGINE_NAME} backtest | {days}d 1h | {len(coins)} coins ===")
results = []
for coin in coins:
    print(f"  fetching {coin}...", end=" ", flush=True)
    bars = fetch_hl_candles_with_funding(coin, days=days, interval='1h')
    if len(bars) < 200 or bars['funding'].isna().all():
        print("no funding data" if bars['funding'].isna().all() else f"insufficient bars ({len(bars)})")
        results.append({"coin": coin, "n_trades": 0, "n_bars": len(bars)})
        continue
    print(f"{len(bars)} bars  →  running...", end=" ", flush=True)
    r = run_backtest(coin, bars, evaluate_latest_bar, TRADE_PARAMS, warmup_bars=50)
    results.append(r)
    if r.get('n_trades', 0) > 0:
        print(f"n={r['n_trades']} WR={r['wr_pct']}% PF={r['pf']} sumR={r['sum_r']}")
    else:
        print("0 fires")
    time.sleep(0.5)

md = sweep_results(results, out_path='BACKTEST_RESULTS.md', engine_name=ENGINE_NAME)
print("\n=== summary ===")
print(md[:2000])
