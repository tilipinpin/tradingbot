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
  --trend-confirmation-samples 3 \
  --confirmation-jump-sigma-multiplier 1.25 \
  --confirmation-min-jump-usd 3.00 \
  --hedge-signal-confirmations 2 \
  --hedge-min-win-probability 0.62 \
  --hedge-fee-rate 0.07 \
  --probability-shrinkage 1.00 \
  --market-data-timeout 3 \
  --min-entry 0.55 \
  --max-entry 0.78 \
  --low-entry-cutoff 0.50 \
  --low-entry-min-win-probability 0.68 \
  --low-entry-confirmation-samples 3 \
  --max-trades 2 \
  --order-size 5
```

真实下单必须额外显式加 `--live-trading`，并在环境变量中配置 `PRIVATE_KEY` 和 `FUNDER_ADDRESS`。
默认不限制会话累计订单数，每个 5 分钟窗口最多成交 2 单；每单仍受 5 份和
3.75 pUSD 本金上限约束。订单被拒、请求异常或返回非 `matched` 状态时会写入摘要并继续，
失败尝试不占用当前窗口的成交额度。
`fair_value_edge` 的正常入场只接受 `0.55–0.78` 的盘口价格并要求至少 62% 模型胜率；
其中 `0.65` 以上继续按价格提高所需 edge。
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
```

先在 Telegram 中主动给机器人发送一条消息，确保 bot 可以向该 chat ID 回复。未配置 token
或 chat ID 时 Telegram 自动关闭；未配置 Webhook 时 Discord 自动关闭，均不影响交易。Discord
只接收启动、停止和正式结算三类彩色 Embed，不接收异常、日报或即时成交，也不提供交易控制命令。`DISCORD_MENTION` 可填写 `<@用户ID>` 或
`<@&角色ID>`，默认允许用户和角色提及；如确需 `@everyone`，将它加入
`DISCORD_ALLOWED_MENTIONS=users,roles,everyone` 并把 `DISCORD_MENTION` 设为 `@everyone`。配置后
Telegram 会发送以下全部通知；Discord 只发送其中的启动、结算和停止：

- 启动：版本、服务器、模式、策略、可用 pUSD、活跃持仓、待赎回价值和估算总权益。
- 成交：实际 `matched` 订单只写入本地成交账本，默认不立即通知；设置 `TELEGRAM_NOTIFY_ON_MATCHED=true` 可恢复即时通知。
- 结算：同一5分钟窗口的所有订单正式结算后只发送一条合并通知，包含逐单明细、窗口总返还、毛盈亏、Taker手续费估算、净盈亏、账户权益和累计战绩；每30秒检查一次，重启后会补查且不会重复推送。
- 异常：签名、余额/授权、RPC/API 超时、网络/代理和订单未成交；持续同类行情异常默认冷却 5 分钟。
- 日报：上海时区午夜后的第一个轮询，统计实际成交、已结算胜率、毛盈亏、手续费估算、净盈亏和账户权益。
- 停止：正常结束、`Ctrl+C` 或未捕获异常，包含运行时长、累计尝试、累计成交、账户权益和最后错误。

同一个已授权 Telegram chat ID 可以直接控制和查询机器人，其他 chat 的消息会被忽略：

- `/balance`：读取可用 pUSD、活跃持仓价值、待赎回价值和估算总权益。
- `/pnl`：读取本地成交账本和逐单结算记录，汇总今日胜率、毛盈亏、手续费估算和净盈亏。
- `/positions`：用 `DEPOSIT_WALLET`/`FUNDER_ADDRESS` 查询 Polymarket Data API 当前持仓。
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
5 分钟窗口生效。纸面和 dry-run 模式可选择全部策略；实盘 Telegram 选择器目前只开放
`fair_value_edge`；`split_maker`、`maker_momentum` 和 `late_favorite`
标记为仅纸面。选择
“跟随启动参数”可清除 Telegram 策略覆盖。

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
  --decision-seconds-before-end 90 \
  --min-seconds-before-end 25 \
  --signal-confirmations 2 \
  --market-data-timeout 3 \
  --min-win-probability 0.62 \
  --low-entry-cutoff 0.50 \
  --low-entry-min-win-probability 0.68 \
  --low-entry-confirmation-samples 3 \
  --edge 0.06 \
  --min-entry 0.45 \
  --max-entry 0.75 \
  --max-spread 0.04 \
  --min-ask-sum 0.90 \
  --max-ask-sum 1.10 \
  --max-trades 1 \
  --max-consecutive-losses 2 \
  --pause-windows-after-losses 2
```

`split_maker` 是库存型纸面做市策略。每个完整窗口开盘10秒后模拟将 5 pUSD Split 为
5 UP + 5 DOWN，再按模型概率和 1.02 的双边目标收入挂出两笔 maker 卖单。模拟挂单至少
静置4秒，且订单簿 best bid 必须实际推进到报价才视为成交，不会因为报价等于 best ask
就假定成交：

```bash
python -m src.watch_updown \
  --slug btc-updown-5m-<timestamp> \
  --duration 28800 \
  --interval 2 \
  --auto-trade \
  --strategy split_maker \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --paper-trading \
  --paper-bankroll 20 \
  --maker-shares 5 \
  --maker-target-pair-sum 1.02 \
  --maker-min-rest-seconds 4 \
  --maker-unpaired-timeout-seconds 10 \
  --maker-cancel-seconds 60 \
  --maker-force-exit-seconds 45 \
  --maker-max-inventory-loss-rate 0.01
```

两边成交后，锁定利润为 `shares × (UP成交价 + DOWN成交价 - 1)`。如果只成交一边，
程序使用完整深度和 taker fee 比较“卖出剩余库存”与“买回已卖方向后 Merge”，在单边
等待10秒、预计库存损失达到1%或进入最后45秒时采用现金回收更高的方案。最后60秒若
尚未成交任何一边，则取消模拟挂单并 Merge 全部库存。窗口结束仍有库存时按正式赢家
结算。该实现只用于保守纸面建模；真实 Relayer Split/Merge、post-only 下单、撤单和 User
WebSocket 成交跟踪完成前，实盘会拒绝选择此策略。

`maker_momentum` v2 是从 Split 做市测试中拆出的实验性纸面策略。它不实际 Split，也不承担
双边库存；程序只维护虚拟 maker 卖价，当 best bid 在挂单静置后真正触达该价格时，将触价
方向视为一次订单流确认。触发会先经过完整预筛，无效触发立即释放候选槽并重新报价，不再
占用10秒等待。候选方向还必须是盘口 favorite、Chainlink 最近3次采样同向、相对开盘价领先
至少0.5 bps，并且追价不超过0.06：

```bash
python -m src.watch_updown \
  --slug btc-updown-5m-<timestamp> \
  --duration 28800 \
  --interval 2 \
  --auto-trade \
  --strategy maker_momentum \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --paper-trading \
  --paper-bankroll 20 \
  --paper-stake 1 \
  --stop-when-bust \
  --momentum-min-entry 0.60 \
  --momentum-max-entry 0.88 \
  --momentum-confirmation-samples 1 \
  --momentum-trigger-timeout-seconds 8 \
  --momentum-min-probability 0.55 \
  --momentum-flow-probability-boost 0.10 \
  --momentum-min-expected-roi 0.03 \
  --momentum-min-lead-bps 0.50 \
  --momentum-strong-expected-roi 0.04 \
  --momentum-strong-lead-bps 2.00 \
  --momentum-max-chase 0.06
```

订单流触发暂按最多0.10的概率增益做纸面假设，调整后仍需达到3%费后预期 ROI。为避免用
一次确认换取低质量频率，每笔还必须满足“费后预期 ROI 至少4%”或“BTC 领先至少2 bps”
其中一项；每个窗口最多一笔。该增益必须通过更长时间的独立样本重新校准，不能从短期回放
直接推断为真实胜率。正式接入 Market/User WebSocket 和验证滑点前，实盘会拒绝此策略。

`late_favorite` v5 是仅纸面的尾盘高置信度策略。它在剩余 55–8 秒观察市场 favorite，
只在市场与 Chainlink 模型同向时考虑入场，并要求 BTC 相对开盘价保留动态价格安全距离。
同一窗口最多一单；亏损结算后下一个窗口继续评估，不启用连败暂停。纸面余额和盈亏会按
Polymarket 加密市场公式扣除 taker fee：

```bash
python -m src.watch_updown \
  --slug btc-updown-5m-<timestamp> \
  --duration 28800 \
  --interval 5 \
  --auto-trade \
  --strategy late_favorite \
  --price-source POLYMARKET_CHAINLINK \
  --ws-proxy socks5h://127.0.0.1:7898 \
  --paper-trading \
  --paper-bankroll 20 \
  --paper-stake 1 \
  --stop-when-bust \
  --max-spot-age 5 \
  --late-entry-start-seconds 55 \
  --late-entry-cutoff-seconds 8 \
  --late-min-entry 0.65 \
  --late-max-entry 0.94 \
  --late-min-win-probability 0.80 \
  --late-min-expected-roi 0.02 \
  --late-fee-rate 0.07 \
  --late-max-spread 0.03 \
  --late-min-ask-sum 0.96 \
  --late-max-ask-sum 1.04 \
  --late-confirmation-samples 2 \
  --late-no-cross-samples 3 \
  --late-signal-confirmations 1 \
  --late-min-lead-bps 1.0 \
  --late-max-pullback-bps 1.50 \
  --late-max-pullback-ratio 0.50 \
  --late-volatility-buffer-multiplier 0.50 \
  --late-pause-windows-after-loss 0
```

最低模型概率只是第一层过滤；每笔还必须覆盖 Taker fee 并保留至少 2% 的模型期望回报。
最近 3 次 Chainlink 采样不得穿越开盘价，最近 2 次必须维持结算方向；当前领先既要达到
1.0 bps，也要覆盖剩余时间波动率的 0.5 倍。相对近期极值的回撤不得超过 1.50 bps 和领先幅度
的 50%。两个 outcome 的 bid/ask 通过一次批量请求读取，避免快速行情中多次请求产生交叉
快照；内部趋势样本通过后只需一次完整信号，避免重复确认耗尽尾盘窗口。策略不会在同一盘口重复加仓。累计足够纸面
样本并验证费后盈亏和回撤前，程序拒绝实盘启用；高命中率不等于保证盈利。

`fair_value_edge` 会根据当前 BTC 价格、起始价、剩余时间和波动率估计出 UP 的理论概率，然后只在原始模型概率相对盘口 ask 有足够 edge 时入场。实盘使用 `--probability-shrinkage 1.00`，但波动率始终不低于长期基准 `0.00005/√秒`，防止短时安静行情制造虚假的极端概率。正常入场要求所选方向 ask 至少为 0.55、最近三次 Chainlink 采样均在同一结算方向且距开盘价没有收窄，并连续出现两次信号；超过动态波动阈值的单次反向跳动会清空确认。首单成交后，第二单既可在相同条件下顺势加仓，也可在模型连续两次翻向且 edge 达标时作为反向保护。保护单使用首单实际成交成本和份数，按保护限价及手续费分别模拟 UP、DOWN 结算，只有组合最大亏损严格下降才会提交。每笔仍需避开最后 25 秒、过期现货价格、宽 spread、交叉报价和异常 ask 总价；高价或临近结算的入场还需要额外 edge。

生产启动器启用 `--official-price-to-beat`：每个窗口使用 Polymarket 发布的官方
`openPrice`，并与本地缓存中最接近精确开盘毫秒时间戳的 Chainlink RTDS 样本核对。
默认要求时间偏移不超过 1000 ms、价格差不超过 0.50 USD；官方值尚未发布、接口限流、
边界样本缺失或校验不一致时不会退回到开盘后的实时价，而是等待或跳过整个窗口。

实盘的每窗口交易上限只统计确认 `matched` 的订单。FOK 拒绝、提交异常、余额或名义金额检查失败都不会占用成交名额，后续有效信号仍会继续尝试。`--duration 0` 表示无限运行。

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
