# Deployment snapshots

These launchers are sanitized backups of the local launchd configurations. They
contain no wallet private key, API token, Telegram token, or Discord webhook.
Runtime secrets remain in the local `.env` file and are intentionally ignored by
Git.

Snapshot captured on 2026-07-18 before enabling official Polymarket `openPrice`
alignment:

- Live launch default: `reversal_four_64`, unlimited duration. Telegram
  offers the currently approved reversal and signal strategies. The former
  `open_060_late_070`, standalone `open_060`, `late_070`, and `late_one_way`
  entries are no longer shown.

The shell launchers accept an optional first argument for the initial event slug.
When omitted, they start from the current five-minute epoch.

In TWAP mode, every strategy that needs a window-open reference starts from the
first Polymarket Chainlink RTDS tick at or after the exact boundary, within the
configured 1000 ms limit. Missing or late ticks fail the window closed. The
official Polymarket Price to Beat is reconciled asynchronously with a 0.50 USD
tolerance and is never fetched synchronously on the order-critical path. Fresh
realtime Chainlink data is still required before any order signal.

The live fair-value launcher uses actual sample timing for volatility and a
long-run floor of 0.00005 per square-root second. Primary entries allow a 0.05
maximum spread. After the first FAK fill, the second slot may add in the same
direction during the normal entry window or protect a reversal in the separate
20-to-1-second window. Protection allows a
0.10 spread and requires at least two valid confirmations spanning five
seconds, an unchanged direction, qualifying edge at every confirmation, and no
more than 0.05 ask-price worsening from the first confirmation. Protection starts
immediately after the first matched fill and stops with three seconds remaining.
BTC must cross the official openPrice by the larger of $2 or a five-second
volatility buffer, and the confirming samples must not narrow back toward the
open. A hedge is
submitted only when a
conservative two-outcome portfolio calculation, including estimated fees,
strictly reduces the maximum loss relative to leaving the first fill
unprotected. Polling accelerates to a one-second cadence for the final 30
seconds.

The provisional boundary may only come from the post-boundary Polymarket
Chainlink RTDS stream; pre-boundary samples, stale cache values, and unrelated
spot sources are forbidden. Once published, the official Price to Beat replaces
the provisional value after background reconciliation. In TWAP markets,
reversal settlement compares the prior window's Price to Beat with its ending
60-second TWAP rather than comparing consecutive TWAP boundary values.
