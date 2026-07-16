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
CHAINLINK_DATA_STREAMS_API_KEY=
CHAINLINK_DATA_STREAMS_API_SECRET=
CHAINLINK_BTC_USD_FEED_ID=
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
FUNDER_ADDRESS=0x... # deployed deposit wallet, not the owner EOA
DEPOSIT_WALLET=0x...
SIGNATURE_TYPE=3
```

V2 新账户必须先通过 Polymarket Builder Relayer 部署 deposit wallet，再把 pUSD
转入该钱包并由该钱包授权交易合约。EOA 中的余额和授权不能用于 deposit wallet
订单。部署和钱包批处理还需要 `RELAYER_URL`、`RPC_URL`、
`RELAYER_API_KEY` 和 `RELAYER_API_KEY_ADDRESS`；CLOB 下单使用 `SIGNATURE_TYPE=3`，且 `FUNDER_ADDRESS`
必须是已部署的 deposit wallet。本项目不会自动转移资金或发起授权。

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

BTC 5m Up/Down 的正式结算源是 Chainlink BTC/USD Data Stream。`watch_updown` 默认严格使用
免费的 `AUTO` 模式，优先订阅 Polymarket 公开的 Chainlink 实时流，失败时再依次尝试
Binance、Coinbase、Kraken 和 CoinGecko。需要代理时传入
`--ws-proxy socks5h://127.0.0.1:7898`，不会回退到交易所价格。也可以显式传入
`--price-source POLYMARKET_CHAINLINK`。

使用付费 Chainlink Data Streams API 时传入 `--price-source CHAINLINK`，并配置
`CHAINLINK_DATA_STREAMS_API_KEY`、`CHAINLINK_DATA_STREAMS_API_SECRET` 和
`CHAINLINK_BTC_USD_FEED_ID`。不要将 Key 或 Secret 提交到 Git。Chainlink 网页展示价格
可能延迟，只适合人工校验，不用于自动入场。

这个命令只 dry-run 打印盘口、fair probability 和理论动作，不会真实下单。

启用自动交易检测，但仍然 dry-run 不下单：

```bash
python -m src.watch_updown \
  --slug https://polymarket.com/zh/event/btc-updown-5m-1783685100 \
  --duration 3600 \
  --interval 10 \
  --auto-trade \
  --decision-seconds-before-end 90 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --market-data-timeout 3 \
  --min-entry 0.45 \
  --max-entry 0.70 \
  --order-size 5
```

真实下单必须额外显式加 `--live-trading`，并在环境变量中配置 `PRIVATE_KEY` 和 `FUNDER_ADDRESS`。
默认不限制会话累计订单数，每个 5 分钟窗口最多尝试 2 单；每单仍受 5 份和
3.50 pUSD 上限约束。订单被拒、请求异常或返回非 `matched` 状态时会写入摘要并继续，
失败尝试仍占用当前窗口的一次额度。
默认 `--duration 0` 持续运行，直到手动停止；可传正秒数设置时限，也可传
`--max-live-orders N` 临时恢复累计订单上限。
实盘进程结束后会写入 `data/live_trade_summary.json`，可运行
`python3 live_trade_summary.py` 查看最后一次会话、订单尝试和 CLOB 响应。

### Telegram 通知

在本机 `.env` 中配置 Telegram BotFather 创建的 bot token 和接收者 chat ID：

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_CHAT_ID=<chat-id>
TELEGRAM_TIMEZONE=Asia/Shanghai
```

先在 Telegram 中主动给机器人发送一条消息，确保 bot 可以向该 chat ID 回复。未配置 token
或 chat ID 时通知模块自动关闭，不影响交易。配置后 `watch_updown` 会发送：

- 启动：版本、服务器、模式、策略和钱包余额。
- 成交：实际 `matched` 订单只写入本地成交账本，默认不立即发送 Telegram；设置 `TELEGRAM_NOTIFY_ON_MATCHED=true` 可恢复即时通知。
- 结算：默认每笔订单只在正式结算后通知一次，包含胜负、返还、本单毛盈亏、收益率、当前余额和累计战绩；每 30 秒检查一次，重启后会补查且不会重复推送。
- 异常：签名、余额/授权、RPC/API 超时、网络/代理和订单未成交；持续同类行情异常默认冷却 5 分钟。
- 日报：上海时区午夜后的第一个轮询，统计实际成交、已结算胜率、策略毛盈亏、余额变化和手续费/余额差额估算。
- 停止：正常结束、`Ctrl+C` 或未捕获异常，包含运行时长、累计尝试、累计成交、最终余额和最后错误。

日报成交账本保存在 `data/live_trade_events.jsonl`，每日余额快照保存在
`data/telegram_daily_state.json`；二者均位于 Git 忽略的 `data/` 目录。断电或 `SIGKILL`
会让进程无法发送停止通知，这是操作系统层面的限制。

纸面账户模拟，初始 20 USDC，每次信号投入 1 USDC，不止盈，余额归零则退出：

```bash
python -m src.watch_updown \
  --slug https://polymarket.com/zh/event/btc-updown-5m-1783685100 \
  --duration 21600 \
  --interval 10 \
  --auto-trade \
  --strategy fair_value_edge \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --paper-trading \
  --paper-bankroll 20 \
  --paper-stake 1 \
  --stop-when-bust \
  --decision-seconds-before-end 90 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --market-data-timeout 3 \
  --min-win-probability 0.62 \
  --edge 0.06 \
  --min-entry 0.45 \
  --max-entry 0.70 \
  --max-spread 0.04 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --max-trades 1 \
  --max-consecutive-losses 2 \
  --pause-windows-after-losses 2
```

开盘动量 + 双边锁利纸面策略：开盘 10 秒后用 0.50 pUSD 建仓，组合成本不超过
0.90 时买入相同份额的反方向；方向反转时按 12% 软止损处理：

```bash
python -m src.watch_updown \
  --slug btc-updown-5m-<timestamp> \
  --duration 1800 \
  --interval 10 \
  --auto-trade \
  --strategy paired_lock \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --paper-trading \
  --paper-bankroll 20 \
  --paired-initial-stake 0.50 \
  --paired-profit-sum 0.90 \
  --paired-emergency-sum 1.05 \
  --paired-stop-loss 0.12
```

`paired_lock` 目前只允许纸面测试；在真实卖出和双腿成交处理完成验证前，程序会拒绝
与 `--live-trading` 同时启用。

三阶段趋势纸面策略把每个 5 分钟窗口分为三个 100 秒区间。前两段按趋势强度标记为
`U`、`D` 或 `N`，默认仅在剩余 100 秒内交易同向延续的 `UU`、`DD`。首轮 6 小时测试中
反转形态 `UD`、`DU` 表现较差，现已默认关闭；只有显式传入
`--three-phase-allow-reversals` 才会重新启用：

```bash
python -m src.watch_updown \
  --slug btc-updown-5m-<timestamp> \
  --duration 1800 \
  --interval 10 \
  --auto-trade \
  --strategy three_phase \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --paper-trading \
  --paper-bankroll 20 \
  --paper-stake 1 \
  --three-phase-edge 0.03 \
  --three-phase-trend-threshold 0.25 \
  --three-phase-reversal-ratio 1.20 \
  --three-phase-min-entry 0.35 \
  --three-phase-max-entry 0.82 \
  --three-phase-confirmations 1 \
  --three-phase-entry-start-seconds 95 \
  --three-phase-entry-cutoff-seconds 40
```

`three_phase` 目前只允许纸面测试。趋势为 `N`、价格未穿越开盘价、盘口价格不在
0.35–0.82、spread 超过 0.04 或理论 edge 低于配置值时都会跳过。第三段开始后先观察
5 秒，只在剩余 95–40 秒的窗口内确认并开仓，最后 40 秒禁止新单。

`fair_value_edge` 会根据当前 BTC 价格、起始价、剩余时间和波动率估计出 UP 的理论概率，然后只在理论概率相对盘口 ask 有足够 edge 时入场。默认要求同方向连续出现两次信号、理论胜率至少 62%，并避开最后 25 秒、过期现货价格、宽 spread、交叉报价和异常 ask 总价。高价或临近结算的入场还需要额外 edge；纸面模拟连续亏损达到阈值后会暂停若干窗口。

## 采集与 walk-forward 回测

只采集 Chainlink BTC 与 Polymarket 实际 bid/ask，不生成交易信号：

```bash
python -m src.watch_updown \
  --slug btc-updown-5m-<timestamp> \
  --duration 604800 \
  --interval 10 \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --record-jsonl data/btc_updown_5m.jsonl
```

采集至少 7 天后，用前 70% 窗口选择参数、后 30% 窗口做一次样本外验证。默认在记录的 ask 上增加 0.01 滑点；`--cost-rate` 可加入额外成本：

```bash
python -m src.replay_recorded data/btc_updown_5m.jsonl \
  --train-ratio 0.70 \
  --slippage 0.01 \
  --cost-rate 0 \
  --min-train-trades 20
```

回放器只接纳开盘采样不晚于 20 秒、收盘采样不早于最后 15 秒的完整窗口，并报告交易数、胜率、净收益、最大回撤和最长连败。若训练集交易数不足，不会选择参数。

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
