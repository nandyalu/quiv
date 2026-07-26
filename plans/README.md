# Implementation plans — Roadmap to v1.0.0

One plan per roadmap phase (see [docs/roadmap.md](../docs/roadmap.md)). Each plan is self-contained: it names the exact files/functions to change, prescribes the design decisions (so the implementer does not have to make them), lists pitfalls, and ends with a verifiable exit checklist.

## Rules for implementers

1. **Do the phases in order.** Later phases assume earlier machinery (e.g. Phase 4 timeout enforcement rides on Phase 2's wake-event loop).
2. **Read the whole plan before writing code.** The Pitfalls section exists because each item was hit or foreseen during design review.
3. **Do not expand scope.** Cron scheduling, durable persistence, event-loop reuse, and lazy logging were explicitly rejected — see "Out of scope" in [docs/roadmap.md](../docs/roadmap.md). In particular, `run_async` must keep creating a **fresh event loop per invocation** (isolation requirement).
4. **Every phase must pass before it ships:**
   ```bash
   uv run pytest            # all tests green
   uv run mypy quiv         # strict mode, zero errors
   uv run zensical build --clean   # docs build clean (when docs change)
   ```
5. **Tests follow the existing conventions**: most scheduler tests use the `running_main_loop` fixture from `tests/conftest.py`; always call `scheduler.shutdown()` in a `finally` block. Keep timing-sensitive tests generous (CI is slow) — poll-with-deadline, never bare `time.sleep` assertions.
6. **When a phase is complete**: bump the version in `pyproject.toml`, add a section to `docs/release-notes.md`, and update the phase status in `docs/roadmap.md`. Update `CLAUDE.md` if the phase changed any pattern documented there (events list, add_task params, etc.).

## Phases

| Phase | Version | Plan | Theme |
|---|---|---|---|
| 1 | v0.5.0 | [phase-1-correctness.md](phase-1-correctness.md) | Bug fixes: double-run guard, registry race, shutdown hardening |
| 2 | v0.6.0 | [phase-2-scheduler-efficiency.md](phase-2-scheduler-efficiency.md) | Smart sleep loop, signature cache |
| 3 | v0.7.0 | [phase-3-db-locking.md](phase-3-db-locking.md) | Finer-grained persistence locking |
| 4 | v0.8.0 | [phase-4-execution-features.md](phase-4-execution-features.md) | Timeout, retry/backoff, jitter |
| 5 | v0.9.0 | [phase-5-management-api.md](phase-5-management-api.md) | update_task, rich queries, stats() |
| 6 | v1.0.0 | [phase-6-release-hardening.md](phase-6-release-hardening.md) | API freeze, docs, benchmarks, soak |
