# Roadmap

This page tracked the work between `v0.4` and `v1.0.0`. Every phase is complete, and `v1.0.0` is the result. The scope was reviewed and frozen on 2026-07-10; the items under [Out of scope](#out-of-scope-for-v100) were considered and deferred, and that section stands as the record of what quiv deliberately does not do. The work after the release continues under [After v1.0.0](#after-v100).

The roadmap was organized into six phases. Each phase shipped independently as its own minor release, and later phases built on machinery from earlier ones. One release, `v0.10.0`, sat outside the phases: the phase numbers stay consecutive, so the version numbers do not.

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

## Phase 6 — v1.0.0 Hardening & Release (`v1.0.0`)

**Status: ✅ complete** — implemented 2026-09-17, ships as `v1.0.0`.

1. API freeze review: a naming pass over the entire public surface (last chance for breaking changes), `__all__` audit, and removal of anything deprecated during the 0.x series.
2. Documentation overhaul: migration notes from 0.x, "Failure handling" and "Observability" pages, an updated README comparison table, and a Simplified Technical English (ASD-STE100) pass over the user-facing docs pages.
3. A committed benchmark suite (dispatch latency, throughput at pool saturation) with numbers published in the release notes.
4. Coverage target of at least 95% and a 24-hour soak test with mixed sync/async/failing/cancelled tasks — zero leaked threads or event loops, and database size bounded by the retention window.

**What it produced.** The three parameters that quiv injects into a handler lost their underscore: `job_id`, `stop_event`, and `progress_hook`. That was the one breaking change, and `add_task()` rejects the old names rather than letting cancellation fail in silence. `TaskNotScheduledError`, deprecated in `v0.9.0`, is gone. Coverage reached 100%, against the 95% target. The benchmark suite lives in [`benchmarks/`](https://github.com/nandyalu/quiv/tree/main/benchmarks), and the 24-hour soak passed: 294,400 jobs, no thread growth, a job history that stopped growing after the first hour, and memory flat at 53 MB. Its log is committed. Decisions from the API freeze, including the alternatives that were rejected, are recorded in [`plans/api-freeze-notes.md`](https://github.com/nandyalu/quiv/blob/main/plans/api-freeze-notes.md).

## After v1.0.0

`v1.0.0` froze the public API. Each phase after it is additive and ships as a minor release in the 1.x series. A change that breaks the 1.0 API waits for `2.0.0`.

The three caveats in the README were reviewed on 2026-09-17: the temporary database, the single process, and the picklable arguments. Process jobs became Phase 7. The other two stay out of scope, and [Out of scope](#out-of-scope-for-v100) records why.

## Phase 7 — Process jobs (`v1.1.0`)

**Status: 📋 planned** — decisions settled 2026-09-17; the plan is [`plans/phase-7-process-jobs.md`](https://github.com/nandyalu/quiv/blob/main/plans/phase-7-process-jobs.md).

quiv keeps its thread pool and gains a pool of processes. A process job runs in a process that quiv spawns for that job alone, and quiv can terminate it. That is the one thing a thread can never offer.

1. **Two pools, one switch per task** — `process_pool_size` joins the configuration, default 0. `add_task()` and `update_task()` gain `executor`, either `"thread"` or `"process"`. A thread pool of size 0 is allowed when the process pool has a size, and then process is the default.
2. **One process per job** — started with the `spawn` method on every platform, never forked, never reused. The handler must be a function that a new process can import by name. A lambda, an inner function, or a bound method is rejected at `add_task()`.
3. **One kill rule** — `process_kill_grace`, default 5 seconds. A stop signal that a process job ignores for that long becomes a terminate. The rule covers `timeout`, `cancel_job()`, `remove_task()`, and `shutdown()`. A thread job is never killed.
4. **The same handler contract** — `job_id`, `stop_event`, `progress_hook`, and `run_on_main()` work in a process job. Progress payloads must be picklable. Everything else stays in the parent: job state, the database, events, retries, and the progress callback.
5. **An inert scheduler in the child** — spawn imports the handler's module, and in a FastAPI app that module builds the scheduler. Inside a worker process `Quiv()` is inert, and its lifecycle methods raise `WorkerProcessError`.
6. **CI on three platforms** — the test suite runs on Linux, macOS, and Windows from this phase on.

**Exit criteria:** every test in the plan passes on all three platforms with coverage at the gate; a 10-minute soak with process jobs leaves no child process behind; the start cost of a process job is measured and published in the release notes; a "Process Jobs" page in the docs.

`forkserver` would cut the start cost of each job on Linux and macOS. It is documented as a later option and not implemented in this phase.

## Out of scope for v1.0.0

The following were reviewed and explicitly deferred:

- **Cron / calendar scheduling** — interval-based scheduling remains the model; the `timezone` parameter stays display-only. `run_at` (v0.10.0) sets the absolute time of a *first* run and does not change this: there is still no calendar expression and no recurrence rule.
- **Durable persistence** — each `Quiv` instance keeps its temporary SQLite database, deleted on `shutdown()`. Reviewed again on 2026-09-17, after the release, and still out. The need was to keep the next run time across a restart. An application can do that itself: store the last run time, compute the next one at startup, and pass it as `delay` or `run_at`. A durable store inside quiv would also have to bring the handler back, and the database never holds the handler.
- **Lambdas and unpicklable arguments** — reviewed on 2026-09-17 and out. `args` and `kwargs` stay pickled, so a lambda, an inner function, or an open connection cannot be an argument. A value that a job must compute at run time belongs inside the handler: pass an id or a name, and resolve it there. Other schedulers stop at the same place. Celery and Dramatiq accept JSON only, and APScheduler with a persistent store needs a function it can import.
- **Work on other machines** — Phase 7 spreads work over processes on one machine. quiv does not become a distributed queue; Celery and arq exist for that.
- **Event-loop reuse for async handlers** — each async invocation keeps its own fresh event loop by design: closing the loop after every job guarantees that leaked `asyncio` tasks or loop state from one job can never bleed into the next. Isolation wins over the micro-optimization.
- **Lazy logging** — eager f-string formatting in log calls stays as-is.
