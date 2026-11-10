#!/usr/bin/env python3
"""下载并缓存 Hyperliquid 行情数据(日线 2 年 / 1h 1 年 / 资金费率)。

用法:
    python scripts/fetch_data.py                # 全量
    python scripts/fetch_data.py --coins BTC,ETH --interval 1d
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qe.config import Config, DEFAULT_UNIVERSE  # noqa: E402
from qe.data import hyperliquid as hl  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="")
    ap.add_argument("--interval", default="")
    ap.add_argument("--funding-days", type=int, default=365)
    args = ap.parse_args()

    cfg = Config.load()
    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else (cfg.universe or DEFAULT_UNIVERSE)
    plan = [(args.interval, {"1d": 730, "1h": 365, "15m": 60}[args.interval])] if args.interval else [("1d", 730), ("1h", 365)]

    # 只保留交易所真实存在的合约
    try:
        live = set(hl.asset_ctxs().index)
        missing = [c for c in coins if c not in live]
        if missing:
            print(f"⚠️ 交易所无此合约,跳过: {missing}")
        coins = [c for c in coins if c in live]
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 无法获取合约列表({e}),按原列表继续")

    ok = fail = 0
    for coin in coins:
        for interval, days in plan:
            try:
                df = hl.candles(coin, interval, days)
                if df.empty:
                    print(f"  ⚠️ {coin} {interval}: 空数据")
                    continue
                hl.save(df, "candles", coin, interval)
                ok += 1
                print(f"  ✅ {coin:6s} {interval:3s} {len(df):5d} 根  {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}")
                time.sleep(0.15)
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  ❌ {coin:6s} {interval:3s} {type(e).__name__}: {str(e)[:80]}")
        # 资金费率
        try:
            fdf = hl.funding_history(coin, args.funding_days)
            if not fdf.empty:
                hl.save(fdf, "funding", coin)
                print(f"  ✅ {coin:6s} funding {len(fdf):6d} 条")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {coin:6s} funding: {str(e)[:60]}")

    print(f"\n[完成] 成功 {ok} 组 K 线 / 失败 {fail}")


if __name__ == "__main__":
    main()
