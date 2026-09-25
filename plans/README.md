# Implementation plans

One plan per roadmap phase (see [docs/roadmap.md](../docs/roadmap.md)). Each plan is self-contained: it names the exact files/functions to change, prescribes the design decisions (so the implementer does not have to make them), lists pitfalls, and ends with a verifiable exit checklist.

Phases 1–6 built `v1.0.0`. Phases from 7 on are the work after the release: each is additive, ships as a minor release in the 1.x series, and changes no 1.0 name or default. A breaking change waits for `2.0.0`.

## Rules for implementers

1. **Do the phases in order.** Later phases assume earlier machinery (e.g. Phase 4 timeout enforcement rides on Phase 2's wake-event loop).

   The phase numbers are consecutive; the version numbers are not. `v0.10.0` shipped between Phase 5 and Phase 6 and has no plan of its own — it was a single additive parameter, not a phase. `v1.1.0` shipped the same way between Phase 6 and Phase 7: `run_at` on `update_task()` and `pending_main_loop_work()`. Read the version column, not the phase number, when you need to know what a release contains.

   **Phases 8 and 9 do not build on Phase 7.** They came out of the 2026-09-25 review of the two applications that run quiv, and both applications need them sooner than process jobs. Ship them in whichever order the applications need, before or after Phase 7. The version in each plan's title is provisional; the version column below is fixed at release, and the phase number never changes.
2. **Read the whole plan before writing code.** The Pitfalls section exists because each item was hit or foreseen during design review.
3. **Do not expand scope.** Cron scheduling, durable persistence, lazy or unpicklable arguments, event-loop reuse, and lazy logging were explicitly rejected — see "Out of scope" in [docs/roadmap.md](../docs/roadmap.md). Durable persistence and lazy arguments were reviewed again on 2026-09-17, after `v1.0.0`, and stay rejected; the roadmap records why. In particular, `run_async` must keep creating a **fresh event loop per invocation** (isolation requirement), in the parent and in a worker process alike.
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
| — | v0.10.0 | no plan | `run_at` absolute-time scheduling ([#66](https://github.com/nandyalu/quiv/issues/66)) — not a phase |
| 6 | v1.0.0 | [phase-6-release-hardening.md](phase-6-release-hardening.md) | API freeze, docs, benchmarks, soak |
| — | v1.1.0 | no plan | `run_at` on `update_task()` ([#79](https://github.com/nandyalu/quiv/pull/79)) and `pending_main_loop_work()` ([#80](https://github.com/nandyalu/quiv/pull/80)) — not a phase |
| 7 | v1.2.0 | [phase-7-process-jobs.md](phase-7-process-jobs.md) | Process jobs: one spawned process per job, a second pool, one kill rule, CI on three platforms |
| 8 | v1.3.0 | [phase-8-waiting-on-work.md](phase-8-waiting-on-work.md) | Waiting on work: `wait_for_job`/`await_job`, `wait_for_task`/`await_task`, `call_on_main()`, `run_subprocess()`, `JobCancelledError` |
| 9 | v1.4.0 | [phase-9-operations.md](phase-9-operations.md) | Operations: `is_running` and `loop_alive`, `run_task_immediately(after_current=True)`, `get_all_tasks(task_name=)`, the "Running in a container" page |
