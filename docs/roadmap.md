# Roadmap to v1.0.0

This page tracks the planned work between the current release and `v1.0.0`. The scope was reviewed and frozen on 2026-07-10; items listed under [Out of scope](#out-of-scope-for-v100) were considered and explicitly deferred.

The roadmap is organized into six phases. Each phase ships independently as its own minor release, and later phases build on machinery introduced by earlier ones.

Detailed implementation plans — one per phase, with prescriptive design decisions, test lists, pitfalls, and exit checklists — live in the repository under [`plans/`](https://github.com/nandyalu/quiv/tree/main/plans).

## Phase 1 — Correctness & Stability (`v0.5.0`)

**Status: ✅ complete** — implemented 2026-07-26, ships as `v0.5.0`.

Small, self-contained bug fixes with no behavioral surprises for existing users.

1. **Double-run guard** — `run_task_immediately()` currently sets a task back to `active` unconditionally. If the task is `running`, the scheduler loop can dispatch a second concurrent job for the same task, breaking the no-overlap invariant; if the task is `paused`, it is silently un-paused. The fix guards on the current status and rejects the request for non-`active` tasks.
2. **Registry race fix** — `remove_task()` called from another thread can remove a handler between the due-task query and dispatch, raising a `KeyError` that stalls the scheduler loop for 5 seconds. Dispatch will skip removed tasks gracefully, and the shared handler/callback/stop-event registries get proper locking.
3. **Shutdown hardening** — `shutdown()` will cancel only `running` jobs (instead of scanning all job history) and accept an optional `timeout` so a hung handler cannot block application shutdown forever. Jobs that fail to drain within the timeout are logged.

**Exit criteria:** regression tests for each bug — concurrent `run_task_immediately` during a running job, `remove_task` racing the dispatch loop, and shutdown with a deliberately hung handler.

## Phase 2 — Scheduler Core Efficiency (`v0.6.0`)

**Status: ✅ complete** — implemented 2026-07-26, ships as `v0.6.0`.

1. **Smart sleep loop** — replace the fixed 1-second polling tick with sleep-until-next-due on an interruptible wait, woken by `add_task()`, `run_task_immediately()`, `resume_task()`, and shutdown. This enables sub-second intervals, removes up to ~1 second of scheduling jitter, and reduces idle database polling to zero. History cleanup keeps its own 60-second cadence via the wait-deadline computation.
2. **Signature cache** — cache each handler's accepted injectable kwargs (`_job_id`, `_stop_event`, `_progress_hook`) so `inspect.signature()` runs once per handler lifetime instead of three times per dispatch.

**Exit criteria:** timing tests proving dispatch latency under 50&nbsp;ms after a task's due time, correct loop wake-up on each mutating API call, and no database queries while the scheduler is idle.

## Phase 3 — Concurrency: Database Locking Rework (`v0.7.0`)

**Status: ✅ complete** — implemented 2026-08-04, ships as `v0.7.0`.

1. **Finer-grained locking** — the persistence layer currently serializes *all* database access behind a single lock, even though WAL mode already allows concurrent readers. Reads will run without the global lock while writes serialize on a writer lock, with `SQLITE_BUSY` / busy-timeout handling.

This is the riskiest change on the roadmap, so it gets its own phase with a dedicated stress-test suite (many concurrent writer and reader threads) run before and after, verifying both correctness and the throughput gain.

**Exit criteria:** stress tests pass at `pool_size=32`; the measured read-path contention improvement is documented in the release notes.

## Phase 4 — Execution Features (`v0.8.0`)

**Status: ✅ complete** — implemented 2026-08-06, ships as `v0.8.0`.

These features all touch the `add_task()` signature and the job-finalize path, and timeout enforcement rides on Phase 2's deadline-aware loop — so they ship together.

1. **Per-task timeout** — a `timeout` parameter on `add_task()`. The scheduler loop tracks running-job deadlines; on expiry it sets the job's stop event so the handler can exit cooperatively, consistent with the existing cancellation model.
2. **Retry / backoff** — `max_retries` and a backoff policy on `add_task()`. On failure with retries remaining, the next run is scheduled by the backoff instead of the normal interval, the attempt is tracked on the job record, and a new `JOB_RETRYING` event is emitted. Run-once tasks are only deleted after retries are exhausted.
3. **Jitter** — an optional `jitter` parameter that adds a random offset to each computed next run, de-synchronizing `fixed_interval` tasks that would otherwise align to the same interval boundaries and fire simultaneously.

**Exit criteria:** timeout fires within one loop wake-up of the deadline; retry sequencing and event emission are covered by tests; the docs gain a "Failure handling" page.

## Phase 5 — Management & Observability API (`v0.9.0`)

**Status: ✅ complete** — implemented 2026-08-06, ships as `v0.9.0`.

1. **`update_task()`** — change a task's interval, args/kwargs, `fixed_interval`, timeout, retry, and jitter settings, or its progress callback, in place — preserving the `task_id` instead of requiring remove-and-re-add. Emits a new `TASK_UPDATED` event and wakes the scheduler loop.
2. **Rich job queries** — `get_all_jobs()` grows `task_id`, time-range, ordering, and `limit`/`offset` parameters (with the same treatment for `get_all_tasks()`), so admin views no longer need to load the entire history into memory.
3. **`stats()`** — a single introspection method returning active job count, pool size and utilization, task counts by status, next due time, and job-history size — ready for dashboards and health checks.

**Exit criteria:** the FastAPI example application gains an admin endpoint exercising all three features.

## `v0.10.0` — Absolute-time scheduling

**Status: ✅ complete** — implemented 2026-09-05, ships as `v0.10.0`.

Not a roadmap phase. `add_task()` gained a `run_at` parameter that takes the absolute time of the first run, as an alternative to `delay` ([#66](https://github.com/nandyalu/quiv/issues/66)). A one-off task is an alarm, and an alarm is naturally set for a time rather than for a duration. The change is additive and keyword-only, so it carried no API-freeze deadline; it shipped ahead of Phase 6 because callers were repeating the same clock arithmetic.

This is not calendar scheduling. `run_at` names one instant for one run. Recurrence stays interval-based — see [Out of scope](#out-of-scope-for-v100).

## Phase 6 — v1.0.0 Hardening & Release

1. API freeze review: a naming pass over the entire public surface (last chance for breaking changes), `__all__` audit, and removal of anything deprecated during the 0.x series.
2. Documentation overhaul: migration notes from 0.x, "Failure handling" and "Observability" pages, an updated README comparison table, and a Simplified Technical English (ASD-STE100) pass over the user-facing docs pages.
3. A committed benchmark suite (dispatch latency, throughput at pool saturation) with numbers published in the release notes.
4. Coverage target of at least 95% and a 24-hour soak test with mixed sync/async/failing/cancelled tasks — zero leaked threads or event loops, and database size bounded by the retention window.

## Out of scope for v1.0.0

The following were reviewed and explicitly deferred:

- **Cron / calendar scheduling** — interval-based scheduling remains the model; the `timezone` parameter stays display-only. `run_at` (v0.10.0) sets the absolute time of a *first* run and does not change this: there is still no calendar expression and no recurrence rule.
- **Durable persistence** — each `Quiv` instance keeps its temporary SQLite database, deleted on `shutdown()`.
- **Event-loop reuse for async handlers** — each async invocation keeps its own fresh event loop by design: closing the loop after every job guarantees that leaked `asyncio` tasks or loop state from one job can never bleed into the next. Isolation wins over the micro-optimization.
- **Lazy logging** — eager f-string formatting in log calls stays as-is.
