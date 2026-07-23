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
  --decision-seconds-before-end 120 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --trend-confirmation-samples 3 \
  --trend-pullback-tolerance-usd 1.00 \
  --trend-pullback-tolerance-percent 25 \
  --confirmation-jump-sigma-multiplier 1.25 \
  --confirmation-min-jump-usd 3.00 \
  --hedge-signal-confirmations 2 \
  --hedge-confirmation-min-seconds 2 \
  --hedge-max-price-worsening 0.05 \
  --hedge-entry-start-seconds 300 \
  --hedge-entry-cutoff-seconds 1 \
  --hedge-open-cross-min-usd 1.00 \
  --hedge-open-cross-sigma-multiplier 1.00 \
  --hedge-market-reversal-threshold 0.55 \
  --hedge-min-win-probability 0.53 \
  --hedge-min-edge 0.01 \
  --hedge-max-spread 0.10 \
  --hedge-fee-rate 0.07 \
  --post-fill-poll-interval 1 \
  --pre-submit-max-adverse-ask-drop 0.02 \
  --probability-shrinkage 1.00 \
  --market-data-timeout 3 \
  --min-entry 0.50 \
  --max-entry 0.78 \
  --low-entry-cutoff 0.55 \
  --low-entry-min-win-probability 0.61 \
  --low-entry-confirmation-samples 3 \
  --max-trades 2 \
  --order-size 5
```

真实下单必须额外显式加 `--live-trading`，并在环境变量中配置 `PRIVATE_KEY` 和 `FUNDER_ADDRESS`。
默认不限制会话累计订单数，每个 5 分钟窗口最多成交 2 单；每单仍受 5 份和
4.05 pUSD 本金上限约束，使 5 份、最高 0.78 的合格信号可以在最多 0.03 滑点下提交。订单被拒、请求异常或返回非 `matched` 状态时会写入摘要并继续，
失败尝试不占用当前窗口的成交额度。
`fair_value_edge` 在 `0.55–0.78` 的盘口价格要求至少 55% 模型胜率和 2% edge，其中 `0.65`
以上继续按价格提高所需 edge。正常档最近 3 次 Chainlink 采样必须始终位于官方开盘价的
所选方向一侧，并允许 `max(1 USD, 本段最大领先距离的 25%)` 的小幅回撤。`0.50–0.55` 使用严格档，要求至少 61%
模型胜率，且最近 3 次采样持续支持所选方向、相对开盘价距离不得收窄。
默认 `--duration 0` 持续运行，直到手动停止；可传正秒数设置时限，也可传
`--max-live-orders N` 临时恢复累计订单上限。
实盘进程结束后会写入 `data/live_trade_summary.json`，可运行
`python3 live_trade_summary.py` 查看最后一次会话、订单尝试和 CLOB 响应。

### Telegram 与 Discord 通知

在本机 `.env` 中配置 Telegram BotFather 创建的 bot token 和接收者 chat ID：

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_CHAT_ID=<chat-id>
TELEGRAM_TIMEZONE=Asia/Shanghai
TELEGRAM_COMMANDS_ENABLED=true

DISCORD_ENABLED=true
DISCORD_WEBHOOK_URL=<discord-webhook-url>
DISCORD_USERNAME=Polymarket Trading Bot
DISCORD_MENTION=
DISCORD_ALLOWED_MENTIONS=users,roles
DISCORD_START_STOP_RETENTION_SECONDS=300
DISCORD_SETTLEMENT_RETENTION_SECONDS=259200
DISCORD_DELETE_RETRY_SECONDS=900
```

先在 Telegram 中主动给机器人发送一条消息，确保 bot 可以向该 chat ID 回复。未配置 token
或 chat ID 时 Telegram 自动关闭；未配置 Webhook 时 Discord 自动关闭，均不影响交易。Discord
接收启动、停止、正式结算和每日日报四类彩色 Embed，不接收异常或即时成交，也不提供交易控制命令。启动和停止消息默认保留5分钟，结算消息保留3天，日报由程序永久保留。到期删除采用持久化事件队列，只在消息到期时请求 Discord；断网失败后默认15分钟再试，进程重启后会恢复队列。`DISCORD_MENTION` 可填写 `<@用户ID>` 或
`<@&角色ID>`，默认允许用户和角色提及；如确需 `@everyone`，将它加入
`DISCORD_ALLOWED_MENTIONS=users,roles,everyone` 并把 `DISCORD_MENTION` 设为 `@everyone`。配置后
Telegram 会发送以下全部通知；Discord 只发送其中的启动、结算、日报和停止：

- 启动：版本、服务器、模式、策略、可用 pUSD、活跃持仓、待赎回价值和估算总权益。
- 成交：实际 `matched` 订单只写入本地成交账本，默认不立即通知；设置 `TELEGRAM_NOTIFY_ON_MATCHED=true` 可恢复即时通知。
- 结算：同一5分钟窗口的所有订单正式结算后只发送一条合并通知，包含逐单明细、窗口总返还、毛盈亏、Taker手续费估算、净盈亏、账户权益和累计战绩；每30秒检查一次，重启后会补查且不会重复推送。
- 异常：签名、余额/授权、RPC/API 超时、网络/代理和订单未成交；持续同类行情异常默认冷却 5 分钟。
- 日报：上海时区午夜后的第一个轮询，统计实际成交、已结算胜率、毛盈亏、手续费估算、净盈亏和账户权益。
- 停止：正常结束、`Ctrl+C` 或未捕获异常，包含运行时长、累计尝试、累计成交、账户权益和最后错误。

同一个已授权 Telegram chat ID 可以直接控制和查询机器人，其他 chat 的消息会被忽略：

- `/balance`：读取可用 pUSD、活跃持仓价值、待赎回价值和估算总权益。
- `/pnl`：读取本地成交账本和逐单结算记录，汇总今日胜率、毛盈亏、手续费估算和净盈亏。
- `/positions`：用 `DEPOSIT_WALLET`/`FUNDER_ADDRESS` 查询 Polymarket Data API，只显示机器人当前 5 分钟窗口的持仓，不混入历史待赎回仓位。
- `/status`：查看进程心跳、运行时间、策略、窗口和累计尝试/成交。
- `/strategy`：查看当前策略并通过内联按钮选择下个窗口使用的策略。
- `/stop`：立即阻止提交新订单并持久化暂停状态；不会撤销已经提交或成交的订单。
- `/start`：清除暂停状态，从下一次有效信号开始恢复下单。
- `/restart`：保存 Telegram offset 和实盘摘要，使用原命令行参数替换并重启当前进程。

启动通知会附带一个 `is_persistent=true` 的 Telegram 大按钮键盘，普通账号在与机器人
私聊时即可使用，不需要 Premium。键盘提供余额、盈亏、持仓、状态、策略、停止、恢复和重启
八个按钮；每次按钮回复都会再次附带键盘。程序还会通过 `setMyCommands` 和
`setChatMenuButton` 保留输入框旁的 `/` 命令菜单作为备用。Reply Keyboard 不用于频道。

策略选择需要二次确认，并持久化到 `data/telegram_daily_state.json`，只在下一个完整
5 分钟窗口生效。纸面、dry-run 和实盘 Telegram 选择器均提供 `fair_value_edge` 与
`late_one_way`。选择“恢复启动策略”可清除 Telegram 策略覆盖，回到启动命令指定的策略。

首次启用命令轮询时会丢弃启动前积压的旧消息，避免历史 `/stop` 或 `/restart` 被误执行。
控制状态和 Telegram offset 也保存在 `data/telegram_daily_state.json`，所以进程重启后不会重复执行指令。

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
  --decision-seconds-before-end 120 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --market-data-timeout 3 \
  --min-win-probability 0.55 \
  --low-entry-cutoff 0.50 \
  --low-entry-min-win-probability 0.61 \
  --low-entry-confirmation-samples 3 \
  --edge 0.02 \
  --min-entry 0.45 \
  --max-entry 0.75 \
  --max-spread 0.04 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --max-trades 1 \
  --max-consecutive-losses 2 \
  --pause-windows-after-losses 2
```

`fair_value_edge` 会根据当前 BTC 价格、起始价、剩余时间和波动率估计出 UP 的理论概率，然后只在剩余 120–25 秒、模型概率相对盘口 ask 有足够 edge 时入场。波动率按每个样本的实际时间间隔计算，并始终不低于长期基准 `0.00005/√秒`。正常入场允许 `0.50–0.78`，模型概率至少 55%、edge 至少 2%；`0.55–0.78` 的三次趋势确认允许回撤 `max(1 USD, 本段最大领先距离的 25%)`，低于 0.55 的严格档要求至少 61% 胜率且仍禁止收窄。正常单最大 spread 为 0.05。确认完成后会重新读取 Chainlink 与双边盘口；所选 ask 下跌超过 0.02、方向改变或 edge 不再达标都会取消旧信号。首单成交后立即改为每秒轮询，第二个名额既可同方向加仓，也可反向保护并持续到剩余 1 秒。反向保护由两条独立路径触发：模型反向概率至少 53% 且 edge 至少 1%，或者反向盘口 bid 连续达到 0.55；盘口路径不再要求模型同步翻向。两条路径都要求 BTC 越过官方 `openPrice`，且距离至少为 `$1` 与两秒短期波动缓冲两者中的较大值。保护最大 spread 为 0.10，并要求同方向确认至少两次、持续至少 2 秒，ask 相对首次确认恶化不超过 0.05。最终仍使用首单实际成交成本和份数计算组合风险，只有最大亏损严格下降才提交。

每个窗口强制使用 Polymarket 发布的官方 `openPrice`（即 Price to Beat），并与本地缓存中
最接近精确开盘毫秒时间戳的 Chainlink RTDS 样本核对。本地现货价格不能替代官方门槛。
默认用 1000 ms 和 0.50 USD 作为边界审计阈值。边界 Chainlink 样本缺失或价差超限时
记录告警但仍使用 Price to Beat。程序至少读取两次相同值且保持 5 秒才采用；读取失败时
会在当前窗口持续重试，不会因进入交易时段而提前跳窗。正式下单前还会再次读取，门槛
缺失则阻止下单，门槛变化则清空本轮行情样本和信号确认后重新建立基准。
真正入场前仍要求实时 Chainlink 报价不超过 20 秒，旧缓存不会用于下单。

实盘默认使用 FAK，配置的最大买入滑点为 0.03，但普通首单只会逐 tick 使用仍能保留最低 edge 的部分；边缘信号可能完全不使用滑点。`0.78` 仍是信号 ask 上限，普通单绝对限价最高为 `0.81`，5 份订单本金硬上限为 4.05 pUSD。盘口可用数量不足时允许部分成交并取消剩余部分。每窗口交易上限只统计确认 `matched` 且有实际成交数量的订单；首仓和保护风险均使用响应中的真实成交成本及份数。提交异常、余额或名义金额检查失败不占用成交名额。`--duration 0` 表示无限运行。结算通知会区分首单、同向加仓和反向保护单。

## 数据采集与历史回测

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
