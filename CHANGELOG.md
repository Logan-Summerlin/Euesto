# Changelog

## Unreleased — Harness P1 gaps

- New read-only Agent tool `status`: every staged change against the publication baseline (created/modified/deleted/permission changes, hashes, sizes, modes), bounded unified diffs, pagination, and the number of publication batches needed. No Git involved.
- New mutation tool `patch`: ordered `write`/`edit`/`delete` operations across files under one checkpoint, all-or-nothing, with per-operation results and failure diagnostics (`max_patch_operations`, `max_patch_bytes`).
- `edit` retries an unmatched multi-line `old_str` once in the file's other line-ending convention (LF ↔ CRLF) and reports typed, bounded diagnostics (`edit.no_match`, `edit.too_many_matches`, `edit.too_few_matches`, `staging.conflict`, `edit.malformed_context`). Failed results can now carry diagnostic `data`.
- The shrink guard is advisory (`shrink_warning: true`) for exact edits and for writes confirmed by a matching `expected_sha256`; unconfirmed whole-file shrinks are still refused.
- Changesets above one broker batch (500 files / 32,000,000 bytes) publish as ordered, separately approved batches; each batch is all-or-nothing on the host, progress is recorded in a durable ledger, and a failed batch can be retried to resume. The staging baseline hand-off now sends a content-free receipt.
- Binary and non-UTF-8 files publish byte-exact through `PublishOperation.content_base64`.
- `visible_files()` reuses digests for unchanged files (stat signature plus a racy-change window), checkpoints no longer re-hash the object store, and write/edit/patch status reuses the checkpoint's walk, so a warm single-file mutation no longer hashes the whole tree.
- Consecutive read-only tool calls in one turn (`read`, `grep`, `find`, `ls`, `status`) run concurrently; mutations stay serialized in order.
- Opt-in allowlisted egress prototype (`docker/compose.egress.yaml`, `egress/`): package installs through an audited CONNECT proxy on an internal-only network; the default profile keeps `network_mode: none`.

## Unreleased — Harness P0 fixes

- Raised the protocol tool-argument cap from 512 KiB to 17,000,000 bytes, derived from the largest argument-carrying hard ceiling plus a fixed envelope and measured as unescaped UTF-8 JSON, so `write` content and `bash` stdin reach their documented limits in every profile.
- `investigate_repository` now resets its four-call allowance at every parent model turn and caps nested wall time at 300 seconds.
- `grep` honors the configured `max_search_seconds`; `find` and `ls` gained the same time budget and report `truncation_reason: "time_budget"`.
- `ls` (and `grep`) now default to the documented 500 results when `max_results` is omitted.
- Added `max_grep_output_bytes` so grep's output clip no longer shares the per-file scan budget.
- A plain `env/` directory is no longer excluded from staging, tools, or publication; `.venv`/`venv` remain excluded.
- `read` now treats metadata/dependency/cache paths hidden from `find`/`grep`/`ls` as missing in both modes.

## Unreleased — Documentation refresh

- Updated all authoritative documentation for the eight-tool API, including `investigate_repository`.
- Added architecture sections for investigation delegation, budget profiles, journaling, skills, and path-safety rules.
- Documented executor tool details (read clamps, shrink guards, bash environment controls) in `docs/TOOLS.md`.
- Documented agent budget profiles and broker publication bounds in `docs/LIMITS.md` and `docs/PUBLICATION.md`.
- Added investigation failure modes to `docs/TROUBLESHOOTING.md`.
- Created `ARCHIVED DOC/` and moved the fully implemented `subagent-tool-plan.md` there.
- Added a concise repository map with per-folder descriptions to `AGENTS.md`.

## Unreleased — Phase 7

- Rebuilt repository documentation around separate architecture, tools, limits, publication, contributor, and troubleshooting guides.
- Documented the current seven-tool API, Plan/Agent permissions, effective resource limits, staging, checkpointing, publication, recovery, and security boundary.
- Reworked the project plan into a status-oriented roadmap rather than a hybrid architecture/history document.
- Added documentation contract tests for the public tool vocabulary and authoritative limit references.
- Updated the README to focus on product behavior, quick start, privacy/data flow, modes, recovery, and developer navigation.
