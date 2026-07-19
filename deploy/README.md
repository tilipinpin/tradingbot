# Deployment snapshots

These launchers are sanitized backups of the local launchd configurations. They
contain no wallet private key, API token, Telegram token, or Discord webhook.
Runtime secrets remain in the local `.env` file and are intentionally ignored by
Git.

Snapshot captured on 2026-07-18 before enabling official Polymarket `openPrice`
alignment:

- Live default: `fair_value_edge`, unlimited duration, at most two matched
  orders per five-minute window. Telegram can switch the next complete window
  to `late_favorite`, which is limited to one matched order per window.
- Paper: `late_favorite` v5, eight hours, 20 pUSD bankroll, 1 pUSD stake.

The shell launchers accept an optional first argument for the initial event slug.
When omitted, they start from the current five-minute epoch.

All current launchers use official Polymarket `openPrice`. The closest cached
Chainlink boundary sample is audited against 1000 ms and 0.50 USD thresholds;
missing or mismatched audit samples emit warnings but no longer reject a window.
Fresh realtime Chainlink data is still required before any order signal.

The live fair-value launcher also requires a selected ask of at least 0.55,
three non-narrowing same-side spot samples, and a long-run volatility floor of
0.00005 per square-root second. Adverse jumps reset signal confirmation. After
the first fill, the second slot may either add with the trend or hedge a
confirmed reversal. Protective entries use a separate 20-to-1-second window;
they can be triggered by two model confirmations or by two consecutive
opposite-side market bids of at least 0.65. A hedge is submitted only when a
conservative two-outcome portfolio calculation, including estimated fees,
strictly reduces the maximum loss relative to leaving the first fill
unprotected. Polling accelerates to a one-second cadence for the final 30
seconds.

Polymarket's timestamped official `openPrice` is the only permitted window-open
value; realtime or cached local spot data can never replace it. The bot keeps
retrying until the selected strategy's entry phase begins (90 seconds remaining
for `fair_value_edge`, or the configured late-strategy entry start). It skips
the window if the official value is still unavailable at that point.
