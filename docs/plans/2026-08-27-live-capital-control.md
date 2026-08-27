# $10K Live Capital Control Implementation Plan

**Goal:** Build a fail-closed strategy sub-ledger and control plane before connecting a real Moomoo account.

## Immutable boundaries

- Initial allocated capital: USD 10,000.
- Maximum strategy market exposure plus active BUY reservations: USD 10,000.
- Loss floor: strategy equity USD 7,500. Crossing it latches a freeze.
- Strategy may sell only quantity acquired by module-tagged, Moomoo-confirmed fills.
- External Moomoo holdings remain read-only and excluded from strategy equity.
- Only US regular trading hours; no pre-market or after-hours execution.
- Secrets, account identifiers and broker references are runtime-only and never committed.
- All broker mutations require durable attempt records and reconciliation.

## State machine

```text
UNCONFIGURED -> FROZEN -> ACTIVE
ACTIVE -> FROZEN (manual, anomaly, stale data, loss floor, exposure breach)
FROZEN -> ACTIVE (explicit authenticated unfreeze, only if all hard gates pass)
FROZEN -> CLEANED (only flat, no active orders; archive first)
CLEANED -> UNCONFIGURED (new strategy must be explicitly provisioned)
```

Loss-floor freeze is latched. It cannot be reset by editing parameters. A human may unfreeze only after strategy equity is above the floor and all reconciliation gates pass.

## Tasks

1. Add `core/live_strategy_control.py` with SQLite schema, strategy equity, owned positions, fill idempotency, immutable limits, parameter versioning, freeze latch, events and archive cleanup.
2. Integrate risk checks into `MoomooClient.preview_order` and `place_order`; SELL is limited to module-owned quantity, BUY exposure is limited to USD 10,000.
3. Extend API with read-authenticated control state/events and control-authenticated freeze/unfreeze/config/cleanup endpoints.
4. Add five-minute sync script that reads Moomoo only, reconciles module-tagged fills, marks owned positions, stores equity and freezes on anomalies.
5. Expand Live Account UI with strategy equity, external vs owned positions, editable hot parameters, freeze control, event timeline and paper overlays.
6. Add paper candidate registry/curve storage and safe cleanup archive.
7. Add deterministic health snapshot/freeze script and research snapshot script for Hermes sidecar jobs.
8. Create jobs paused until the read-only Moomoo acceptance gate is completed.
9. Test secrets, hard limits, loss floor, ownership, concurrency, parameter versioning, cleanup, APIs and browser rendering.
