# Event graph validation

Measured locally on 2026-09-14 with Python 3.13 and Node 22. The scale dataset is synthetic and the UI screenshots come from synthetic fixtures. These are reproducible engineering checks, not a measured guarantee of a companion's future recall or conversation quality.

| Check | Result |
|---|---|
| Python regression suite | 600 passed; 3 opt-in tests deselected |
| Host/transport tests | 79 passed |
| Browser workflows | 7 passed, including sent-body and receipt lookup |
| DSH compatibility | 136 tests passed |
| TypeScript SDK | Build and 3 tests passed; generated from 50 API operations |
| Scale population | 50,000 events, 1,000 works, 10,000 disclosure records |
| First graph page | 150 nodes in 69.32 ms |
| Explicit old-event query | 40.06 ms |
| Chat context construction | 285.51 ms; 798 injected tokens within an 800-token limit |
| Extra model calls in scale/ordinary-chat probe | 0 |

The measured population took 3.36 seconds in one transaction. The benchmark queries return bounded pages; full graph data and debugging envelopes are not injected into chat. Cache invalidation tests cover source correction, new receipts and relation revision. Per-batch compression checkpoints support recovery without regenerating completed batches.

The focused regression cases cover accepted and uncertain receipts, partial sharing, cross-channel rewording, duplicate mode, concurrent reservations, body-bound references, source correction, mapping retraction, finding versions, merge/split undo, transactional rollback and per-input deliberate silence. Task-lock regressions cover running tools, background tasks, pending delivery and restarts.

Live duplicate-sharing rate, semantic false-merge rate and long-term cache-hit improvements require subsequent traffic. They are not inferred from synthetic tests. Private case replay and deployment receipts are kept in the private operating environment rather than this repository.
