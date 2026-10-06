# Roadmap

This page tracked the work between `v0.4` and `v1.0.0`. Every phase is complete, and `v1.0.0` is the result. The scope was reviewed and frozen on 2026-07-10; the items under [Out of scope](#out-of-scope-for-v100) were considered and deferred, and that section stands as the record of what quiv deliberately does not do. The work after the release continues under [After v1.0.0](#after-v100).

The roadmap was organized into six phases. Each phase shipped independently as its own minor release, and later phases built on machinery from earlier ones. Two releases, `v0.10.0` and `v1.1.0`, sat outside the phases: the phase numbers stay consecutive, so the version numbers do not.

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

On 2026-09-25 the two applications that run quiv, trailarr and ten-acre, were reviewed against their running containers. Neither had logged a scheduler fault, and both carried the same workarounds: handlers that hand all their work to the main loop and leave quiv nothing to record, endpoints that add a one-off and cannot report it, a cancel that cannot reach a child process, a `shutdown()` with no timeout inside a ten-second stop grace, no way to ask whether the loop is alive, and tests that stub quiv out. That review produced Phases 8 and 9. They do not build on Phase 7, and they ship before it: Phase 8 as `v1.2.0`, Phase 9 as `v1.4.0`, and Phase 7 as `v1.5.0`. The phase numbers stay; the versions moved. `v1.3.0` went to the fixes for #86, which is why Phase 9 is not `v1.3.0`.

## `v1.1.0` — `run_at` on `update_task()` and `pending_main_loop_work()`

**Status: ✅ complete** — implemented 2026-09-24, ships as `v1.1.0`.

Not a roadmap phase. Two additive changes shipped ahead of Phase 7. `update_task()` gained `run_at`, so the next run of a task moves to an absolute time and the task keeps its `task_id` ([#79](https://github.com/nandyalu/quiv/pull/79)). `pending_main_loop_work()` returns how many callables handed to the main loop by `run_on_main()` have not finished, and `shutdown()` warns when it leaves any behind ([#80](https://github.com/nandyalu/quiv/pull/80)).

quiv never waits for that work and never cancels it, because the loop belongs to the application. A `shutdown` that drained the work was written and then removed during review. It could hang in three ways, and one case has no fix: a job abandoned by `shutdown(timeout=...)` runs on a thread that cannot be stopped, so it can hand work over after any drain has finished. The count is the feature. `CLAUDE.md` records the decision so that a drain is not proposed again.

## Phase 7 — Process jobs (`v1.5.0`)

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

## Phase 8 — Waiting on work (`v1.2.0`)

**Status: ✅ complete** — implemented 2026-09-25, ships as `v1.2.0`. The plan is [`plans/phase-8-waiting-on-work.md`](https://github.com/nandyalu/quiv/blob/main/plans/phase-8-waiting-on-work.md). It does not depend on Phase 7 and shipped before it.

Everything here lets a caller wait on something and lets cancellation reach it.

1. **Wait for a job** — `wait_for_job(job_id, timeout)` and `wait_for_task(task_id, timeout)` return the finalized `Job`; `await_job` and `await_task` are their async forms for code on the application's loop. A test can drive a real scheduler, and an endpoint that adds a one-off can report its result.
2. **`call_on_main()`** — the sibling of `run_on_main()` that waits for the result. The job then carries the real duration and failure, `JOB_FAILED` fires, retries apply, and the task's `timeout` cancels the coroutine. A handler that is one hop to the main loop becomes tracked work.
3. **`run_subprocess()`** — a drop-in for `subprocess.run` inside a handler. A stop signal terminates the child and kills it after a grace, the same rule Phase 7 applies to a process job. A timeout keeps the stdlib exception.
4. **`JobCancelledError`** — raised by both helpers when the stop event fires while they wait; the handler lets it propagate and the job finalizes as `CANCELLED`. **`SchedulerStoppedError`** — what a waiter gets when `shutdown()` returns first.

**Exit criteria:** the tests in the plan pass on 3.10 through 3.14; `docs/testing.md` opens with a working recipe for testing an application's own handlers against a real instance.

## `v1.3.0` — One error costs one job

**Status: ✅ complete** — implemented 2026-10-04 to 2026-10-06, ships as `v1.3.0`.

Not a roadmap phase. This release fixes [#86](https://github.com/nandyalu/quiv/issues/86). Several single errors left a task `running` for good, lost a worker slot, or stopped dispatch until the process restarted. The [release notes](release-notes.md) list each fix. Nine decisions came out of the work. Decisions 1 to 4 were made on 2026-10-04, after the report. Decisions 5 to 9 were made on 2026-10-06, during the review of the pull request.

1. **`quiv_job.task_id` is not a foreign key.** The job history stays after its task row is deleted. Some applications set `PRAGMA foreign_keys=ON` on every SQLAlchemy engine in the process, and that setting reaches the engine of quiv. With the key, the delete of a finished run-once task failed. This was the fault behind the report. `ondelete="SET NULL"` was considered and not used: it makes `Job.task_id` optional, which changes a public type, and the job loses the task that it belonged to.
2. **A run time past year 9999 is clamped, not refused.** Every computed run time goes through one function, which clamps it to 30 December 9999 (UTC). A value that worked before behaves the same. A ceiling on the retry backoff, for example one day, was considered and not adopted. The docs promise that the delay doubles with each failure, so a ceiling is a separate decision.
3. **`NaN` is refused.** The numeric parameters of `add_task()` and `update_task()` raise `ConfigurationError` for `NaN`. A `NaN` interval had the same result as an overflow: the task stayed `running`.
4. **One failure is not recovered, and the design of its fix is chosen.** After the handler returns, quiv writes the task back to `active`. If that write fails, the task stays `running` until the process restarts. quiv logs the error and frees the worker. After decisions 2 and 3, the write fails only when the system fails, for example on a full disk or an I/O error. This risk was accepted for `v1.3.0`, and no release is planned for the fix.

    The database alone cannot fix the task. Only `finalize_task_after_job` returns a task to `active`, and a stuck row looks the same as a task whose job still runs. Only the memory of quiv knows which is which. That memory is reliable, because the database is a private file and quiv is its only writer. If recovery is built later, it uses this design:

    - **Chosen: write it again.** `_run_job` keeps the values that it tried to write: the job id, the start time, whether the job failed, the status, and the error. The loop writes them again, with a backoff, until the write succeeds. The next run time and any retry that is due come out exact. The design costs nothing while no write fails, so an idle scheduler still sends no query. Three rules apply. The handler of a run-once task stays registered until the write succeeds; otherwise a retry would find no handler, and the loop would skip it on every pass. A waiter waits for the write, or gets a `Job` made from the values that quiv tried to write; it never gets a job that still says `running`. At shutdown, quiv drops the writes that are still pending.
    - **Rejected: a periodic check of `running` rows.** The check would compare the `running` rows with a set of tasks in memory that have a job in flight. It races the normal path. If the set and the row change in the wrong order, the check sees a dispatch in progress as stuck and starts a second run of the task. The check must also guess whether the last job failed, so a retry that is due is lost. A drain for `run_on_main` work failed review in the same way, on 2026-09-20.

    Until then, an application can find a stuck task: `stats().tasks_by_status.get("running", 0)` stays higher than `stats().active_jobs`. `remove_task()` followed by `add_task()` clears it.

5. **A pause holds while a job runs.** `pause_task()` on a task with a running job sets the task to `paused`, and the task stays `paused` when the job ends. Before, the end of the job set the task back to `active`. Two more writes that set `active` without a check are now conditional. The return to `active` after a failed dispatch changes only a `running` row, and `mark_task_running` refuses a row that is not `active`. Each of these writes holds the persistence write lock.
6. **`resume_task()` raises `TaskRunningError` while a job of the task runs.** A resume at that time made the task due, and the loop started a second run beside the job. The row of a task paused during its job says `paused`, so quiv keeps in memory the ids of the tasks that have a job in flight. A dispatch adds the id before it marks the row `running`. The job removes the id after it returns the task to the schedule. The job rows were not used for this check: after a failed `finalize_job`, a row says `running` for good, and its task could never be resumed.

    - **Rejected for now: resume after the job ends, with no error.** To do this safely, the row needs a separate pause flag, and the status stays `running` until the job ends. That adds a column and changes what `get_task()` shows after `pause_task()`. Phase 9 adds `rerun_requested`, a flag of the same kind, so the idea comes back there.

7. **`TaskRunningError` inherits `TaskNotActiveError`.** `run_task_immediately()` raised `TaskNotActiveError` for a running task, and an existing `except` clause must still catch that error. A separate class would break the clause, which a 1.x release must not do.
8. **After a failed write, quiv hands over the job that it tried to write.** If `finalize_job` fails, or the read-back fails, the `JOB_*` event and the waiters get a `Job` made in memory: the status, the duration, the error, and the attempt. A first version gave the waiters the database error and sent no event. The caller then could not know whether its handler had succeeded, it had to catch a SQLAlchemy exception from a quiv method, and the listeners did not see the end of the job. The new behavior follows the rule for waiters in the design that decision 4 chose.
9. **The release is `v1.3.0`, not `v1.2.1`.** It adds a public class, and `resume_task()` now raises for a task with a running job. Semantic versioning makes that a minor release. Phase 9 moved to `v1.4.0`, and Phase 7 moved to `v1.5.0`.

## Phase 9 — Operations (`v1.4.0`)

**Status: 📋 planned** — decisions settled 2026-09-25; the plan is [`plans/phase-9-operations.md`](https://github.com/nandyalu/quiv/blob/main/plans/phase-9-operations.md). Does not depend on Phase 7 or Phase 8; ships after Phase 8 and before Phase 7.

1. **Health** — `is_running` and `QuivStats.loop_alive`: `start()` was called, `shutdown()` was not, and the loop thread is alive. Cheap enough for a probe.
2. **`run_task_immediately(after_current=True)`** — a running recurring task runs again as soon as its current job finalizes, instead of the call raising.
3. **`get_all_tasks(task_name=...)`** — an exact-match filter, for "does a one-off with this name already exist".
4. **A "Running in a container" page** — the shutdown timeout under the stop grace, the temporary database and `TMPDIR`, logging, the restart practice both applications arrived at, the daily-at-a-time recipe, and health checks. It can ship as a docs-only commit ahead of the code.

**Exit criteria:** the tests in the plan pass; the container page is in the nav; the soak script reads `is_running`.

## Out of scope for v1.0.0

The following were reviewed and explicitly deferred:

- **Cron / calendar scheduling** — interval-based scheduling remains the model; the `timezone` parameter stays display-only. `run_at` (v0.10.0) sets the absolute time of a *first* run and does not change this: there is still no calendar expression and no recurrence rule.
- **Durable persistence** — each `Quiv` instance keeps its temporary SQLite database, deleted on `shutdown()`. Reviewed again on 2026-09-17, after the release, and still out. The need was to keep the next run time across a restart. An application can do that itself: store the last run time, compute the next one at startup, and pass it as `delay` or `run_at`. A durable store inside quiv would also have to bring the handler back, and the database never holds the handler.
- **Lambdas and unpicklable arguments** — reviewed on 2026-09-17 and out. `args` and `kwargs` stay pickled, so a lambda, an inner function, or an open connection cannot be an argument. A value that a job must compute at run time belongs inside the handler: pass an id or a name, and resolve it there. Other schedulers stop at the same place. Celery and Dramatiq accept JSON only, and APScheduler with a persistent store needs a function it can import.
- **Work on other machines** — Phase 7 spreads work over processes on one machine. quiv does not become a distributed queue; Celery and arq exist for that.
- **Waiting for `run_on_main` work at shutdown** — reviewed on 2026-09-20 and out. A drain was written and removed: it could hang in three ways, and a job abandoned by `shutdown(timeout=...)` can hand work over after any drain has finished. quiv reports the count with `pending_main_loop_work()` and the application decides when its loop may close. Phase 8's `call_on_main()` makes the work part of the job instead.
- **Event-loop reuse for async handlers** — each async invocation keeps its own fresh event loop by design: closing the loop after every job guarantees that leaked `asyncio` tasks or loop state from one job can never bleed into the next. Isolation wins over the micro-optimization.
- **Lazy logging** — eager f-string formatting in log calls stays as-is.
