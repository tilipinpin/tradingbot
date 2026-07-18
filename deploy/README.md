# Deployment snapshots

These launchers are sanitized backups of the local launchd configurations. They
contain no wallet private key, API token, Telegram token, or Discord webhook.
Runtime secrets remain in the local `.env` file and are intentionally ignored by
Git.

Snapshot captured on 2026-07-18 before enabling official Polymarket `openPrice`
alignment:

- Live: `fair_value_edge`, unlimited duration, at most two matched orders per
  five-minute window.
- Paper: `late_favorite` v5, eight hours, 20 pUSD bankroll, 1 pUSD stake.
- Paper: `maker_momentum` v2, eight hours, 20 pUSD bankroll, 1 pUSD stake.

The shell launchers accept an optional first argument for the initial event slug.
When omitted, they start from the current five-minute epoch.

All current launchers require official Polymarket `openPrice` alignment. A window
is eligible only when the closest cached Chainlink sample is within 1000 ms of
the exact boundary and differs from `openPrice` by no more than 0.50 USD.

The live fair-value launcher also requires a selected ask of at least 0.55,
three non-narrowing same-side spot samples, and a long-run volatility floor of
0.00005 per square-root second. Adverse jumps reset signal confirmation. After
the first fill, the second slot may either add with the trend or hedge a
confirmed model reversal.
