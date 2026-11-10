# crypto-quant-engine

Hyperliquid 永续合约的**量化交易引擎**:数据 → 回测 → 参数稳健性检验 → 纸交易/实盘。

- 零外部数据依赖:只用 Hyperliquid 公开 `/info` 接口(Binance/Bybit/OKX 从大陆服务器**不可达**,Hyperliquid 可达)
- 自带成本模型:taker/maker 手续费 + 滑点 + **资金费率**(永续必须算)
- 防未来函数:信号 t 收盘生成、t→t+1 收益生效(有单测覆盖)
- 支持 4 个策略:横截面动量 / 短期反转 / 资金费套利 / 趋势跟踪
- 纸交易与实盘共用同一套目标权重逻辑

## 快速开始

```bash
uv venv .venv && source .venv/bin/activate
uv pip install pandas numpy pyyaml pytest hyperliquid-python-sdk

python cli.py fetch                # 下载行情(日线 2 年 / 1h 7 月 / 资金费率 1 年)
python cli.py backtest             # 三策略对比回测
python cli.py sweep                # 参数扫描 + 训练/测试分割
python cli.py robustness           # 参数平台 + 留一法(关键!)
python cli.py paper --once         # 纸交易跑一轮
python cli.py live  --once         # 真实下单(需 API 钱包)
```

## 实测结论(2025-11 → 2026-11,含全部成本)

| 策略 | 全段 CAGR | Sharpe | 最大回撤 | 稳健性 |
|---|---|---|---|---|
| **BTC+ETH 趋势(10/50)** | **+42.7%** | **1.35** | -12.6% | ✅ 参数平台扎实 |
| 趋势 20 币分散 | +11.8% | 0.53 | -21.6% | ⚠️ 某折 -31% |
| 资金费套利(beta 中性) | +48.6%* | 2.13* | -9.2% | ❌ 留一法 -8%~+20% |
| 横截面动量 | -27.3% | -0.89 | -30.3% | ❌ |
| 短期反转(1h) | -28% | -6.9 | -99% | ❌ 手续费吃光 |

\* 资金费策略的漂亮数字**通不过稳健性检验**:剔除任意一个币,CAGR 在 -8.2% ~ +19.8% 之间摆动 → 判定为**过拟合,不上实盘**。

### 趋势策略的参数平台(为什么信它)

| fast\slow | 30 | **50** | 100 |
|---|---|---|---|
| 5 | +2.3% | **+52.0%** | -7.9% |
| 10 | -8.0% | **+42.7%** | -16.1% |
| 20 | +11.1% | **+32.0%** | -27.1% |

慢线 = 50 时,快线 5/10/20 **全部 +32%~+52%** —— 这是"平台"而非"尖峰",可信度高得多。

## 架构

```
qe/
├── config.py            配置/成本/常量
├── metrics.py           CAGR/Sharpe/Sortino/MaxDD/Calmar/月度
├── data/
│   ├── hyperliquid.py   客户端(K线/资金费/快照)+ gzip 缓存
│   └── panel.py         多币种面板拼装(按近期覆盖率筛选)
├── backtest/engine.py   权重型回测:成本 + 资金费 + 杠杆裁剪
├── strategies/library.py 4 个策略(统一"组合波动率目标"定仓)
└── live/
    ├── executor.py      纸交易 / Hyperliquid 真实下单
    └── runner.py        常驻循环(每 N 分钟调仓)
scripts/                 fetch / backtest / sweep / robustness / candidates
tests/                   引擎会计正确性 + 防未来函数(6 项全过)
```

## 实盘配置

```bash
export HL_ACCOUNT_ADDRESS=0x...     # 主钱包地址
export HL_PRIVATE_KEY=0x...         # **API 钱包**私钥(仅交易权限,不能提币)
python cli.py live --once           # 先跑一次核对
python cli.py live --interval-min 60
```

安全:私钥只从环境变量读,不进 git、不进日志;默认 `paper` 模式;下单带 1% 滑点保护。

## 已知局限

1. **数据源单一**:Hyperliquid 可达,其他主流交易所从大陆服务器全部被墙
2. **资金费历史只有 1 年**(接口限制),价格 2 年
3. **未做资金费套利的多交易所对冲**(需要能访问 Binance/OKX)
4. 回测未含**爆仓/强平**逻辑(实盘需自行控制杠杆,建议 ≤1x)
5. 趋势策略在慢线 100 上失效 → 它赚的是**中周期趋势**,不是长趋势
