# Polymarket BTC Trading Bot

一个面向 Polymarket BTC 涨跌市场的 Python 交易机器人骨架。

默认模式是 `DRY_RUN=true`，只会扫描市场、生成交易意图并打印日志，不会真实下单。真实资金交易有亏损和合规风险，请先小额测试，并确认你所在地区允许使用相关服务。

## 功能

- 扫描 Polymarket Gamma API 中活跃、未关闭的 BTC/Bitcoin 市场
- 默认只筛选包含 up/down/higher/lower/above/below 等方向词的涨跌盘
- 支持通过 `POLYMARKET_EVENT_SLUG` 锁定 event，并自动筛选 event 下的 BTC 涨跌 markets
- 支持通过 `POLYMARKET_MARKET_SLUG` 锁定单个 market
- `POLYMARKET_EVENT_SLUG` / `POLYMARKET_MARKET_SLUG` 可以填 slug，也可以直接粘 Polymarket URL
- 按流动性排序候选 markets，并支持限制每轮最多处理数量
- 读取 Yes/No 的 CLOB token id
- 可从 BTC 现货价格和市场问题中的美元阈值生成方向信号
- 默认 dry-run，真实下单需要显式开启 `LIVE_TRADING=true`
- 下单前有价格、单笔金额、每日金额、市场关键词等安全检查

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp .env.example .env
```

## 配置

编辑 `.env`：

```bash
DRY_RUN=true
LIVE_TRADING=false
POLYMARKET_EVENT_SLUG=
POLYMARKET_MARKET_SLUG=
TRADE_OUTCOME=AUTO
SPOT_PRICE_SOURCE=COINGECKO
MANUAL_BTC_PRICE=
THRESHOLD_BUFFER_BPS=25
MAX_PRICE=0.55
ORDER_SIZE=5
MAX_DAILY_USD=25
MIN_MARKET_LIQUIDITY=0
MAX_MARKETS_PER_RUN=3
MARKET_QUERY=bitcoin btc
MARKET_DIRECTION_QUERY=up down higher lower above below
```

真实下单还需要：

```bash
LIVE_TRADING=true
DRY_RUN=false
PRIVATE_KEY=0x...
FUNDER_ADDRESS=0x...
SIGNATURE_TYPE=0
```

新用户通常应优先阅读 Polymarket 的 deposit wallet/signature type 文档；本项目不替你处理入金、授权或资金划转。

## 运行

扫描并 dry-run：

```bash
python -m src.bot
```

运行测试：

```bash
pytest -q
```

运行 BTC 5m Up/Down fair-value dry-run 模拟：

```bash
python -m src.simulate_updown --duration 300 --interval 15 --window 300
```

如果要模拟盘口价并输出理论动作：

```bash
python -m src.simulate_updown --duration 300 --interval 15 --up-ask 0.52 --down-ask 0.52
```

盯住真实 Polymarket BTC 5m Up/Down 窗口，并在窗口结束后自动寻找下一个 `+300` 秒 slug：

```bash
python -m src.watch_updown --slug https://polymarket.com/zh/event/btc-updown-5m-1783685100 --duration 900 --interval 10
```

这个命令只 dry-run 打印盘口、fair probability 和理论动作，不会真实下单。

启用自动交易检测，但仍然 dry-run 不下单：

```bash
python -m src.watch_updown \
  --slug https://polymarket.com/zh/event/btc-updown-5m-1783685100 \
  --duration 3600 \
  --interval 10 \
  --auto-trade \
  --decision-seconds-before-end 120 \
  --min-entry 0.40 \
  --max-entry 0.50 \
  --order-size 5
```

真实下单必须额外显式加 `--live-trading`，并在环境变量中配置 `PRIVATE_KEY` 和 `FUNDER_ADDRESS`。

纸面账户模拟，初始 20 USDC，每次信号投入 1 USDC，不止盈，余额归零则退出：

```bash
python -m src.watch_updown \
  --slug https://polymarket.com/zh/event/btc-updown-5m-1783685100 \
  --duration 21600 \
  --interval 10 \
  --auto-trade \
  --paper-trading \
  --paper-bankroll 20 \
  --paper-stake 1 \
  --stop-when-bust \
  --decision-seconds-before-end 120 \
  --min-entry 0.40 \
  --max-entry 0.50 \
  --max-trades 1
```

回测 BTC 5m Up/Down 历史盘口策略：

```bash
python -m src.backtest_updown --latest-slug btc-updown-5m-1783685100 --windows 100 --decision-seconds-before-end 60
```

可选输出逐笔结果：

```bash
python -m src.backtest_updown --latest-slug btc-updown-5m-1783685100 --windows 100 --output backtest.csv
```

指定市场：

```bash
POLYMARKET_MARKET_SLUG=bitcoin-up-or-down-july-9 python -m src.bot
```

指定 event，并从 event 下自动筛选 BTC 涨跌子市场：

```bash
POLYMARKET_EVENT_SLUG=bitcoin-up-or-down-july-9 python -m src.bot
```

也可以直接粘 URL：

```bash
POLYMARKET_EVENT_SLUG=https://polymarket.com/event/bitcoin-up-or-down-july-9 python -m src.bot
```

如果同时设置 `POLYMARKET_MARKET_SLUG` 和 `POLYMARKET_EVENT_SLUG`，机器人会优先使用单个 market slug。

为了降低误扫和低流动性成交风险，建议从保守配置开始：

```bash
MIN_MARKET_LIQUIDITY=100
MAX_MARKETS_PER_RUN=1
```

## 策略说明

当前策略非常保守：当 `TRADE_OUTCOME=AUTO` 时，它会尝试从市场问题里解析美元阈值，例如 `$110,000`，再用公开 BTC/USD 现货价格判断买 YES 还是 NO。只有现价离阈值超过 `THRESHOLD_BUFFER_BPS`，且目标 outcome 的 best ask 小于等于 `MAX_PRICE` 时，才生成买入意图。如果交易所价格接口在你的网络里不可用，可以设置 `MANUAL_BTC_PRICE` 手动提供 BTC/USD 价格。

它不是收益策略，只是一个安全的机器人骨架。你可以在 `src/strategy.py` 或 `src/price_signal.py` 中替换为自己的信号，例如：

- BTC 现货动量
- Polymarket order book 价差
- 与期权隐含概率比较
- 只交易特定到期时间的 BTC Up/Down 市场

## 风险

- Polymarket 市场可能有地域、KYC、合规限制。
- 预测市场价格不等于真实概率，且流动性、滑点、手续费、结算风险都会影响结果。
- 私钥不要提交到 Git，也不要发给任何人。
- 先 dry-run，再小额真实测试。
