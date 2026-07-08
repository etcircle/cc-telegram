# `taskn_queue_shapes_v2.1.198.jsonl`

Fixture for the CC 2.1.198 **queue-shaped `<task-notification>` close** fix
(`temp/2026-07-08-queued-taskn-close-plan.md`). Pins the parser/monitor/scan
behavior against the exact JSONL shapes CC writes when a background task
completes while the parent is BUSY.

## Provenance

CC delivers a completed background task's `<task-notification>` in one of two
shapes depending on parent state, and BOTH are preceded by a
`queue-operation` / `enqueue` line that carries the full envelope in top-level
`content` (written at COMPLETION time):

1. **Parent idle → `type:"user"` delivery** (works pre-fix). The enqueue line,
   then ~74 ms later a `{"type":"user", … "origin":{"kind":"task-notification"}}`
   entry (DELIVERY timestamp) — the monitor's existing extraction branch catches
   the user entry.
2. **Parent busy → attachment delivery** (MISSED pre-fix). The SAME enqueue
   line, then a `{"type":"attachment","attachment":{"commandMode":"task-notification",…}}`
   entry at the same COMPLETION timestamp — NO `type:"user"` entry EVER follows.
   `transcript_parser.parse_entries`' hard `type not in (user,assistant)` gate
   dropped both lines, so the GH #44 `background_agents` key never tombstoned
   and typing stranded to the 2 h `is_background` TTL.

The fix anchors on the `queue-operation` / `enqueue` line (present in BOTH
shapes) as the canonical close signal, distinguishing its **COMPLETION
timestamp** (queue-op) from the user-entry **DELIVERY timestamp**.

## Lines (0-indexed)

REDACTED-REAL lines are minimally redacted copies of real incident lines from
`~/.claude/projects/-Users-felixcardix-dev-workspaces-di-copilot/35e7e0b1-50a1-455c-bd51-3a58a9a5d172.jsonl`
(CC 2.1.198). Redaction (Hermes r3 P2-4 — the repo may be shared): session ids,
`parentUuid`/`uuid`/`promptId`/`sourceToolAssistantUUID`, local paths, `cwd`,
`gitBranch`, `slug`, and work-summary prose → placeholder equivalents. The JSON
shape, entry types, and the load-bearing fields
(`type`/`operation`/`content`/`attachment`/`commandMode`/`toolUseResult`/
`backgroundTaskId`/timestamps, plus the `<task-id>` join keys) are kept faithful.

| # | label | source line | shape |
|---|-------|-------------|-------|
| 0 | REDACTED-REAL | 1573 | worked pair — queue-op close, task `b5y24hagb` |
| 1 | REDACTED-REAL | 1579 | worked pair — `type:"user"` delivery, task `b5y24hagb` |
| 2 | REDACTED-REAL | 1609 | missed trio — Bash launch tool_result (`backgroundTaskId` `bihtr1tc7`) |
| 3 | REDACTED-REAL | 1625 | missed trio — queue-op close, task `bihtr1tc7` |
| 4 | REDACTED-REAL | 1630 | missed trio — attachment delivery, task `bihtr1tc7` (no user entry follows) |
| 5 | SYNTHETIC | — | non-notification queued command (a user message typed while busy) → no entry |
| 6 | SYNTHETIC | — | parity negative: leading whitespace before the envelope → no entry |
| 7 | SYNTHETIC | — | parity negative: missing `</task-notification>` close tag → no entry |
| 8 | SYNTHETIC | — | parity negative: malformed open tag → no entry |
| 9 | SYNTHETIC | — | parity negative: non-whitespace suffix after the close tag → no entry |
| 10 | SYNTHETIC | — | adversarial: envelope whose summary embeds `Task ID: wf_bogus` + `agentId: abogus123` → close extracted, NO launch minted |

SYNTHETIC lines carry a `_fixture` sidecar key (which the parser ignores) so
they are self-labeling in the file; only the REDACTED-REAL lines carry CC
2.1.198 evidence weight. REDACTED-REAL lines carry NO sidecar (structure kept
faithful) and are identified by their intrinsic fields.

## Audit evidence (the enqueue-anchor invariant)

Full-incident audit of the source transcript (2026-07-08; the local path above;
verified independently by Hermes r3): every task-notification delivery in the
incident transcript has a prior same-id `queue-operation` / `enqueue` line.

| delivery shape | count | with a prior same-id `queue-operation`/`enqueue` |
|----------------|-------|--------------------------------------------------|
| `type:"user"` (DELIVERY ts) | 45 | 45 (100%) |
| `type:"attachment"` (COMPLETION ts) | 14 | 14 (100%) |

This is a **CC 2.1.198 OBSERVED invariant**, version-pinned like the other
TUI/JSONL drift pins — NOT claimed universal. Attachment-ONLY delivery (an
attachment with no prior enqueue) is a documented UNSUPPORTED shape (pinned by
a no-entry test); re-audit at the next CC version bump. Real duplicate
ENQUEUE-only pairs also exist in the source (`b4ey2yxbc` lines 968/970,
`bup05myk6` lines 1215/1216) — the enqueue line is idempotent (`rec.completed`
is a set), pinned by the duplicate-idempotency test.
