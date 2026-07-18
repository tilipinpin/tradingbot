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
