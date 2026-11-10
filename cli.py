#!/usr/bin/env python3
"""统一命令行入口。

    python cli.py fetch                     # 下载/更新行情
    python cli.py backtest [--interval 1d]  # 回测各策略
    python cli.py sweep                     # 参数扫描 + 训练/测试分割
    python cli.py robustness                # 参数平台 + 留一法
    python cli.py paper   [--once]          # 纸交易常驻
    python cli.py live    [--once]          # 真实下单(需 API 钱包)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

COMMANDS = {
    "fetch": ["scripts/fetch_data.py"],
    "backtest": ["scripts/backtest.py"],
    "sweep": ["scripts/sweep.py"],
    "robustness": ["scripts/robustness.py"],
    "candidates": ["scripts/candidates.py"],
    "paper": ["-m", "qe.live.runner", "--mode", "paper"],
    "live": ["-m", "qe.live.runner", "--mode", "live"],
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"未知命令 {cmd}\n{__doc__}")
        sys.exit(1)
    args = COMMANDS[cmd] + sys.argv[2:]
    raise SystemExit(subprocess.call([PY] + args, cwd=ROOT))


if __name__ == "__main__":
    main()
