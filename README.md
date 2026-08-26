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

V2 新账户必须先通过 Polymarket Relayer 部署 deposit wallet，再把 pUSD
转入该钱包并由该钱包授权交易合约。EOA 中的余额和授权不能用于 deposit wallet
订单。部署和钱包批处理还需要 `RELAYER_URL`、`RPC_URL`、
`RELAYER_API_KEY` 和 `RELAYER_API_KEY_ADDRESS`；CLOB 下单使用 `SIGNATURE_TYPE=3`，且 `FUNDER_ADDRESS`
必须是已部署的 deposit wallet。本项目不会自动转移资金或发起授权。

Polygon 完整份额拆分使用官方统一版 `polymarket-client` 的 `SecureClient`，直接复用
上述 Relayer API key/address 与现有 deposit wallet，不再依赖 Builder API
key/secret/passphrase 三凭据接口。

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
- 日报：北京时间早上 08:00 后的第一个轮询发送一次，统计周期为北京时间 08:00 至次日 08:00；账户、成交、盈亏与反转策略轮次统计合并为同一份 Telegram/Discord 日报。
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
私聊时即可使用，不需要 Premium。键盘提供余额、盈亏、持仓、状态、策略、手动交易、停止和
恢复按钮；每次按钮回复都会再次附带键盘。`/restart` 仍保留为命令行入口。程序还会通过 `setMyCommands` 和
`setChatMenuButton` 保留输入框旁的 `/` 命令菜单作为备用。Reply Keyboard 不用于频道。

“手动交易”使用独立异步执行队列，不改变自动策略的轮次、阶段、趋势结果或订单计数。
本窗口可每次买入 UP 或 DOWN 2份，重复点击即可追加；卖出只清空该窗口、该方向由手动模式
实际买到的全部可用份额，不会卖出自动策略持仓。下一窗口只允许预排买入 UP 或 DOWN 2份，
并在目标窗口开盘后执行一次。所有订单均需二次确认并使用实时盘口 FAK；请求及提交状态会
持久化，重启后恢复未提交的排队订单，对提交状态不确定的订单不自动重试。手动与自动模式
仍共用钱包余额和链上/CLOB实际持仓，因此手动成交会自然改变账户可用资金。

策略选择需要二次确认，并持久化到 `data/telegram_daily_state.json`，只在下一个完整
5 分钟窗口生效。程序默认策略为 `reversal_three_16`；实盘 Telegram 选择器还提供
`reversal_v11`，以及首阶段分别
要求连续四或六个同向窗口的 `reversal_v11_four_streak`、
`reversal_v11_six_streak`，以及 `fair_value_edge`、
`momentum_confirmation`、`smart_score`、`ewma_twap_fair`、`fast_directional_hedge_simple` 和
`reversal_four_64` 和 `reversal_three_16`。
反转轮次尚未结束时，切换会延后到整轮结束，避免中途遗留递增状态。选择“恢复启动策略”
可清除 Telegram 策略覆盖，回到启动命令指定的策略。

`fast_directional_hedge_simple` 是独立的“盘口波动套利”V2.0可选策略，切换前默认不生效。
它以Polymarket可执行盘口作为入场主信号，不再等待BTC相对Price to Beat同方向移动确认；
BTC数据仅保留在程序级行情陈旧和异常风控中。领先侧ask处于0.53～0.60且连续两个不同盘口
Tick保持优势后，以固定小仓位FAK入场；信号至提交最多允许0.02追价，允许部分成交。持仓后
持续按实际数量计算可卖Bid VWAP，扣除预计买卖双边Taker手续费后，每份净利润达到0.02
pUSD即FAK卖出赚取波动价差。未达到止盈时仍保留初始、移动及三级快速止损；止损完全由
可执行盘口触发，不再等待BTC确认，并在卖出原仓与买入反向Token之间选择净回收更高的
风险退出路径。每窗口最多两次独立入场，剩余30秒停止开仓，风险管理持续到结算。状态和
执行事件分别保存在
`data/fast_directional_hedge_simple_state.json` 与 `data/fast_directional_hedge_simple_events.jsonl`。

`ewma_twap_fair` 是独立的 EWMA·TWAP60 公平价策略，切换前默认不生效。它只在
TWAP60 结算规则下运行，用原始 Chainlink 现货按真实采样间隔估算每秒 EWMA 波动率，
并与最近60秒实现波动率按 70%/30% 混合。最终末60秒均价的有效方差时钟为
`剩余秒数 - 40`；策略只在剩余300～75秒开仓。信号必须覆盖1.5%模型优势、实际Taker
手续费、0.25%半点差和0.30%滑点缓冲，再按四分之一Kelly配置仓位，单窗口一单、单笔
本金最高25 pUSD。边界、TWAP或原始Chainlink现货缺失时失败关闭。

`reversal_v11` 在 TWAP 规则下用刚结束窗口的固定 Price to Beat 与窗口结束处的
Chainlink 60 秒 TWAP 比较，立即推导结果；绝不再用相邻两个 TWAP 边界互相比。
新窗口开盘先采用边界后首个合格 RTDS tick 作为临时 Price to Beat，官方值在后台复核，
不阻塞当前窗口的关键下单路径。每个暂定结果都会进入待复核队列；Gamma 后续发布
正式结果后才核对，一致则记录通过，不一致则只暂停当前窗口并通知，不设置全局暂停。
`reversal_v11`（5窗反转·10阶）和 `reversal_v11_four_streak`
（4窗反转·10阶）均为十阶段。第1阶段使用1份计划量和首阶段盘口/RV规则；
第2～10阶段以2/4/8/16（其后最低16份）为最低计划量，同时按实时ask、实际成交成本及
Taker手续费重新计算能够追回本轮此前全部实际亏损的份数，取两者较大值。追回阶段不使用
概率、edge、公允价值、开盘穿越、RV或普通市场过滤。余额不足以完整执行追回单时，本窗口
不提交缩量订单。第10阶段失败后不结束本轮，而是停留在第10阶段继续按累计实际亏损
重算完整追回单，直到反转成功；只有余额不足以完整执行下一笔追回单时才停止并锁定本轮。
策略不设置盈利暂停，
完成一轮后继续滚动判断后续窗口。状态保存在
`data/reversal_v11_state.json`，窗口结算继续复用
公允价值策略的盈利/亏损模板，并同时向 Telegram 和 Discord 推送。

`reversal_v11_six_streak`（兼容保留的内部 ID，电报显示“4窗反转·4-0-0追回”）
采用4/0/0/1/2/4/8/16/32/64十阶段：第2、3阶段只观察；第4阶段的1 pUSD是后段
追回序列基线；第5阶段起只追回第4阶段以来的累计实际亏损，不把前面的4/0/0计入。
第5～10阶段不使用策略过滤；第10阶段失败后继续重复第10阶段。只有反转成功或余额不足以
完整追回时才结束本轮，余额不足时不提交缩量订单。

`reversal_three_4_8`（兼容保留的内部 ID，电报显示“2窗反转·12阶”）是独立可选实盘
策略：最近两个紧邻窗口同向后，在下一窗口买反向侧；各阶段本金上限依次为
1/2/2/2/2/1/2/4/8/16/32/64 pUSD。十二阶段均要求反向侧实时可成交 ask 严格低于
0.49；等于0.49也不下单。前五个不规则阶段不计入后段追回：第6阶段的1 pUSD作为
新基线，第7阶段起只按第6阶段以来的累计实际亏损全额追回，并跳过0.49价格及其他
策略过滤。第12阶段失败后继续重复第12阶段，直到反转成功或余额不足以完整追回；后者才会
结束并锁定该趋势。实际份数按下单时ask换算，并向下适配交易所金额
精度，因此实际投入可能略低于对应阶段上限。
名义1 pUSD的第1和第6阶段是例外：由于交易所要求可成交BUY至少1.01 pUSD且maker
金额为两位小数，程序会按当前价格取最小的合法订单，实际投入可能略高于1 pUSD。

实盘启动自检要求 CLOB 认证、至少足够执行下一阶段的可用余额（新轮次至少满足
1.01 pUSD的交易所可成交下限）、
可读取双边持仓且没有未完成订单。后续动态追回仓位仍以提交时的实际可用余额为约束。
日报发送后会等待窗口结束和当前反转轮次结束，再执行安全重启。

首次启用命令轮询时会丢弃启动前积压的旧消息，避免历史 `/stop` 或 `/restart` 被误执行。
控制状态和 Telegram offset 也保存在 `data/telegram_daily_state.json`，所以进程重启后不会重复执行指令。

日报成交账本保存在 `data/live_trade_events.jsonl`，每日余额快照保存在
`data/telegram_daily_state.json`；二者均位于 Git 忽略的 `data/` 目录。断电或 `SIGKILL`
会让进程无法发送停止通知，这是操作系统层面的限制。

`momentum_confirmation`（电报显示“动量确认”）独立实现动量方案A，并可直接切换实盘。它只在剩余
270～25秒时评估：BTC 相对开盘至少移动0.04%或20美元（取较大者），最近30秒继续
同向，所选方向盘口 bid 强于反向，且 ask 位于0.45～0.75；它不再叠加模型概率或edge。

`reversal_four_64` 是可实盘选择的“4窗反转·64U”：最近连续四个已结算
窗口同向时，在下一窗口启动标准反转轮次；阶段递增、累计亏损追回、结果确认和通知沿用
标准反转执行器。64 pUSD为本轮累计实际亏损软警戒；结算一笔失败订单后，累计实际亏损
达到或超过64 pUSD即立即结束并锁定该趋势，不再追加追回单。第1阶段仍以1份为最低计划量；第2阶段起，实际计划份数取固定阶段份数
（2/4/8/16，后续阶段最低16份）与“按实时ask和Taker手续费计算、若本单获胜可使本轮
预计净盈亏不低于0”所需份数中的较大值，且追回阶段不使用策略过滤。余额不足以完整执行保本单时，
立即结束并锁定该趋势，不提交无法保本的缩量订单。每次提交前都会按本轮累计
实际亏损、当前阶段已成交成本与手续费、剩余计划订单的最坏成本和手续费重新计算。

`reversal_three_16` 是默认的“3窗反转·16U”：最近连续三个已结算窗口同向时，
在下一窗口启动纯反转轮次，不再包含公允价值差二选一入口。阶段递增与累计亏损追回沿用
`reversal_four_64`；16 pUSD 为本轮累计实际亏损软上限，结算失败后达到或超过该值即
结束并锁定当前趋势，不再追加追回单。首阶段仍使用盘口、spread、深度及 RV60/RV300
过滤；第2阶段起按本轮累计实际亏损计算完整追回单，余额不足时不缩量下单。

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

`fair_value_edge` 会根据当前 BTC 价格、起始价、剩余时间和波动率估计理论概率，但短时交易方向改由当前同一个5分钟窗口内的Polymarket盘口决定。程序对最近3个UP/DOWN最优ask及其真实采样时间做线性回归：目标侧ask斜率至少 `+0.003/秒`、相对另一侧的斜率至少 `+0.005/秒`，并且从近期高点回撤不超过 `0.01`。盘口先选方向，模型只检查该方向在实际ask与Taker手续费后是否仍有足够edge；BTC方向样本、跨窗口价格和BTC反向跳动不再参与主单趋势确认，跨窗口样本仅用于波动率估计。策略只在剩余120–25秒入场，正常价格为 `0.50–0.78`，模型概率至少55%、edge至少2%；低于0.55的严格档要求至少61%模型概率。正常单最大spread为0.05。预提交会用最新盘口替换最后一个斜率样本，并重新检查方向、概率和edge，盘口斜率反转或优势消失即取消。首单成交后的反向保护逻辑保持独立不变。

BTC 样本严格分组：最近 300 秒的滚动样本可以跨五分钟窗口，但只用于估算波动率；
方向、连续趋势确认、动量确认和智能评分中的趋势分量在每个新窗口都会清空，只使用当前
窗口相对当前 `openPrice` 的样本。跨窗口价格不得参与方向判定。

反转首阶段的FAK在签名提交前会强制刷新一次完整盘口，并以该次快照的可成交ask下单。
若FAK零成交，程序以0.15秒间隔立即刷新并重试，整窗最多仍为3次；每次重试都会重新检查
盘口深度、spread和0.64的首阶段价格上限，超过上限后立即停止，不追高。

TWAP 规则下，所有依赖开盘基准的策略统一读取精确窗口边界之后的首个 Polymarket
Chainlink RTDS tick，默认要求其距离边界不超过 1000 ms，并立即冻结为临时
Price to Beat。边界 tick 缺失或过期时本窗口失败关闭，不使用边界前样本、旧缓存或其他
现货源替代。Polymarket 发布的正式 Price to Beat 由后台异步获取，签名前不再同步请求；
正式值与临时值差异不超过 0.50 USD 时直接确认，超限且尚未成交时清空本窗口样本并改用
正式值，已有敞口时停止新增风险订单但仍允许必要的退出处理。反转策略结算上一窗口时，
使用“上一窗口 Price to Beat 对比结束处 60 秒 TWAP”，Gamma 正式结果随后复核。
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
