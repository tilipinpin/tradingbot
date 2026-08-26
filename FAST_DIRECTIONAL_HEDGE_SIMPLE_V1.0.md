# Fast Directional Hedge Simple V1.0

> 本文档保留为历史基线。当前同一内部策略标识已经升级为
> [盘口波动套利 V2.0](BOOK_VOLATILITY_ARBITRAGE_V2.0.md)。

## Identity

- `strategy_id`: `fast_directional_hedge_simple`
- `version`: `1.0`
- default: disabled; enabled only through the normal strategy selector
- scope: Polymarket BTC five-minute UP/DOWN markets

This strategy follows a short-lived directional advantage visible in the
executable Polymarket book. It deliberately does not use Fair Probability,
OBI, Trade Flow, RSI/MACD, Martingale sizing, active pair arbitrage, partial
profit locking, or repeated same-direction averaging.

## Entry

1. Compare executable UP and DOWN asks.
2. The side with the higher ask is the candidate leader.
3. Require two distinct valid book updates with the same leader; do not add a
   fixed time delay.
4. Require the candidate ask to be in `[0.53, 0.60]`.
5. Record the first qualifying ask as `SignalPrice`.
6. Reject the entry when `LatestAsk - SignalPrice > 0.02`.
7. Submit a fixed-size FAK BUY with at most `0.02` entry slippage and allow a
   partial fill. Never chase the unfilled remainder beyond the entry cap.
8. Stop opening new entries with 30 seconds remaining. A window may contain at
   most two independent entries; hedge orders do not consume this allowance.

## Position management

- Hold a correct directional position to settlement; do not actively take
  profit and do not buy the opposite token because the pair cost is below one.
- Calculate risk from the executable bid VWAP for the actual filled quantity.
- Initial stop: `EntryPrice * (1 - 0.20)`.
- `PeakPrice` only increases.
- Start trailing protection after an absolute gain of `0.10`.
- Once trailing starts, enable break-even at `EntryPrice + 0.00`.
- Trailing stop: `PeakPrice * (1 - 0.15)`.
- Effective stop is the maximum of initial, break-even, previous effective,
  and trailing stops, and therefore never decreases.

## Stop priority

| Mode | Trigger | Confirmation |
| --- | --- | --- |
| EMERGENCY | executable price penetrates stop by at least `0.04` | immediate |
| FAST | absolute move over 500 ms is at least `0.05` | one tick |
| NORMAL | ordinary executable-price stop breach | two ticks |

Priority is `EMERGENCY > FAST > NORMAL`. Stop/risk management remains active
after new entries close and through the end of the window.

## Hedge

After a confirmed stop, the only permitted opposite-side action is an equal
quantity BUY used to remove directional exposure. The order path is kept short:

`Stop -> opposite asks/depth -> FAK BUY -> fill accounting -> remaining exposure`

- Hedge slippage is independently configurable; V1.0 defaults to `0.05`.
- FAK partial fills are accepted.
- Every retry is sized from `EntryQty - HedgeQty`; it must never exceed the
  remaining directional exposure.
- A returned order failure may retry. An exception with uncertain submission
  status freezes further submissions and requires reconciliation, preventing a
  duplicate opposite order.
- When UP and DOWN quantities match, state becomes `HEDGED` and directional
  management ends until settlement.

## Persistence and records

State is atomically persisted after candidate, fill, peak/stop, trigger,
submission, and hedge changes. Event records identify strategy/version and
contain entry fills, stop type/speed, penetration, hedge fills, quantities,
prices, pair cost, and timestamps needed for execution and PnL reporting.

The operational implementation is in
`src/fast_directional_hedge_simple.py`; CLI parameters use the `--fdh-*`
prefix and the strategy is selectable from Telegram as “快速方向对冲·简版”.
