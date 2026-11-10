"""实盘/纸交易常驻循环:每 N 分钟按策略目标权重调仓。

    python -m qe.live.runner --mode paper --strategy trend_btc_eth
    python -m qe.live.runner --mode live  --strategy trend_btc_eth --once   # 先跑一次验证
"""
from __future__ import annotations

import argparse
import json
import time
import traceback
from datetime import datetime, timezone

import pandas as pd

from ..config import Config, DATA_DIR
from ..data import hyperliquid as hl
from ..strategies import library as S
from .executor import make_executor, save_state

STATE_DIR = DATA_DIR / "live"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def build_targets(strategy: str, cfg: Config, live_mids: dict[str, float]) -> tuple[dict[str, float], float]:
    """用日线 + 实时价(作为当日临时收盘)算目标权重,返回 {coin: 目标名义金额} 与总权益。"""
    coins = ["BTC", "ETH"]
    frames = {}
    for c in coins:
        df = hl.candles(c, "1d", days=cfg.lookback_days)
        if df.empty:
            raise RuntimeError(f"{c} 日线为空")
        frames[c] = df
    px = pd.DataFrame({c: d["close"] for c, d in frames.items()}).sort_index()
    # 追加实时价作为今日临时 bar
    now = pd.Timestamp.now(tz="UTC").normalize()
    live = pd.Series({c: live_mids.get(c, px[c].iloc[-1]) for c in coins}, name=now)
    px = pd.concat([px[px.index < now], live.to_frame().T])

    if strategy == "trend_btc_eth":
        w = S.trend_following(px, fast=cfg.trend_fast, slow=cfg.trend_slow,
                              rebalance=1, vol_target=cfg.target_vol)
    else:
        raise ValueError(f"未知策略 {strategy}")

    last = w.iloc[-1]
    equity = cfg.initial_equity
    return {c: float(last.get(c, 0.0)) * equity * cfg.max_leverage / max(cfg.max_leverage, 1)
            for c in coins}, equity


def run_once(strategy: str, mode: str, cfg: Config) -> dict:
    ex = make_executor(mode, cfg.initial_equity)
    mids = hl.all_mids()
    targets, equity = build_targets(strategy, cfg, mids)
    if mode == "live":
        equity = ex.equity()
    trades = []
    for coin, notional in targets.items():
        mid = mids.get(coin)
        if not mid:
            continue
        t = ex.submit(coin, notional, mid)
        if t:
            trades.append(t)
            print(f"  {t['side']:4s} {coin:4s} {t['size']:>10.6f} @ {t['px']:,.2f} ({t['notional']:,.0f} USD)")
    state = {"strategy": strategy, "mode": mode, "ts": datetime.now(timezone.utc).isoformat(),
             "equity": equity, "targets": targets, "mids": {k: mids.get(k) for k in targets},
             "trades": trades}
    save_state(strategy, state)
    return state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="trend_btc_eth")
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--interval-min", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg = Config.load()
    print(f"策略 {args.strategy} · 模式 {args.mode} · 间隔 {args.interval_min} 分钟")
    if args.mode == "live":
        print("⚠️  实盘模式:将真实下单。")

    while True:
        t0 = time.time()
        try:
            st = run_once(args.strategy, args.mode, cfg)
            print(f"[{st['ts']}] 权益 ${st['equity']:,.2f} · 目标 " +
                  " ".join(f"{k}={v:,.0f}" for k, v in st["targets"].items()) +
                  f" · 成交 {len(st['trades'])} 笔")
        except Exception:  # noqa: BLE001
            print("[错误] 本轮失败:\n" + traceback.format_exc(limit=3))
        if args.once:
            break
        sleep = max(60, args.interval_min * 60 - (time.time() - t0))
        time.sleep(sleep)


if __name__ == "__main__":
    main()
