# Phase 9 — Operations (v1.3.0)

This phase is what running quiv in two containers taught. Neither application can ask quiv whether it is healthy; the soak script reads the loop thread's liveness through an undocumented attribute. Both applications call `shutdown()` with no timeout inside Docker's default ten-second stop grace, and one of them runs nine-minute jobs. Both reinvented the same restart practice: re-add tasks at boot, stagger the delays, restore one-off times from their own store. trailarr checks for an existing one-off by scanning every task's name, and both applications hit "run it again after the current run" and had to drop the request.

Three small code additions and one documentation page, all additive: `is_running` and `QuivStats.loop_alive`, `run_task_immediately(after_current=True)`, `get_all_tasks(task_name=...)`, and a "Running in a container" page with the restart and daily-at-a-time recipes. **This phase does not depend on Phase 7 or Phase 8.** The order decided on 2026-09-25 is Phase 8, then this phase as `v1.3.0`, then Phase 7.

**Decisions settled on 2026-09-25** (do not reopen):

- Health is a boolean, not a status enum. `Quiv.is_running` and `QuivStats.loop_alive` both mean: `start()` was called, `shutdown()` was not, and the loop thread is alive. The loop catches `Exception` and continues, so a dead loop thread means something outside `Exception` killed it, which is exactly what a health check must catch.
- `run_task_immediately(task_id, *, after_current=False)`. The default is unchanged and still raises `TaskNotActiveError` for a running task. With `after_current=True`, a running recurring task is flagged and runs again as soon as the current job finalizes, whatever that job's outcome. A running run-once task still raises, because nothing remains to run. A paused task still raises.
- `get_all_tasks(task_name=...)` is an exact-match filter and combines with the existing `status`, `include_run_once`, `limit`, and `offset`. Names may still repeat; that is the 1.0 contract.
- The container page describes practice, and it can ship as a docs-only commit before the code in this phase. Documentation first, code second.
- No new config value, no new event, no new exception.

---

## 1. Health

### `quiv/base.py`

`QuivBase.__init__` gains `self._started = False`. `start()` sets it to `True` before starting the thread.

```python
@property
def is_running(self) -> bool:
    """True while the scheduler loop runs.

    ``start()`` was called, ``shutdown()`` was not, and the loop thread
    is alive. Cheap: no database access.
    """

    return self._started and not self._shutdown and self.thread.is_alive()
```

`stats()` passes `loop_alive=self.is_running`.

### `quiv/models.py`

`QuivStats` gains one field, last, with a default so positional construction elsewhere stays valid:

```python
loop_alive: bool = False
```

Docstring: "Whether the scheduler loop thread is running; the same value as ``Quiv.is_running``."

### Docs

`docs/observability.md` gains a "Health" section with a FastAPI endpoint that returns 503 when `scheduler.is_running` is false, and notes that `stats()` reads the database while `is_running` does not, so a health probe should use `is_running`. The container page links here.

---

## 2. `after_current`

### `quiv/models.py`

`TaskDB` gains `rerun_requested: bool = False`. The public `Task` model exposes the same field, so a UI can show that a run is queued. Both are additive; the temp database is created fresh per instance, so there is no migration.

### `quiv/persistence.py`

`queue_task_for_immediate_run(task_id, after_current=False)`:

- `ACTIVE`: unchanged.
- `RUNNING` and `after_current` and not `run_once`: set `rerun_requested = True`, commit, return 1.
- `RUNNING` and `after_current` and `run_once`: raise `TaskNotActiveError("Task '...' is a running run-once task; nothing remains to run again.")`.
- `RUNNING` without `after_current`, and `PAUSED`: unchanged messages.

`finalize_task_after_job`: read `existing.rerun_requested` once at the top. After the retry branch and the interval branch have set `next_run_at`, and before the jitter, apply the flag:

```python
if rerun_requested:
    existing.next_run_at = now
    existing.rerun_requested = False
```

The flag wins over the retry backoff and over the interval, because the caller asked for a run, and jitter is not applied to it. The run-once branch deletes the row and never sees the flag. The retry counter still increments on a failure, so `max_retries` keeps its meaning.

The write that `pause_task()` performs clears `rerun_requested` in the same transaction. Without that, a task paused with a pending rerun would run twice on resume: once from `resume_task()` and once from the flag.

### `quiv/base.py`

`run_task_immediately(self, task_id: str, *, after_current: bool = False) -> int` forwards the flag. The log line says "queued to run again after the current job" when the flag was used. `docs/api.md` documents the four outcomes in a table.

### `quiv/scheduler.py`

Nothing. `finalize_task_after_job` already wakes the loop through `_run_job`, and `next_run_at = now` is due on the next pass.

---

## 3. `get_all_tasks(task_name=...)`

`quiv/persistence.py` `get_all_tasks(..., task_name: str | None = None)`: `if task_name is not None: stmt = stmt.where(TaskDB.task_name == task_name)`. A bare value comparison, per the predicate convention. `quiv/base.py` `get_all_tasks` gains the same keyword and passes it through. `docs/api.md` adds one line and the trailarr use case: "does a one-off with this name already exist" is `get_all_tasks(task_name=name, include_run_once=True)`.

---

## 4. `docs/containers.md` (new), nav entry "Running in a Container" after "Bigger Applications"

Write it in the order an operator meets the problems.

1. **Stopping.** Docker sends SIGTERM, waits `stop_grace_period` (default 10 s), then SIGKILL; Kubernetes uses `terminationGracePeriodSeconds` (default 30 s). `shutdown()` with no timeout waits for every running job. Pass `shutdown(timeout=...)` smaller than the grace, and set the grace to fit your longest acceptable wait. Show the compose key. Say what an abandoned job does afterwards: it may log errors and recreate the temporary database file, as the API page already says. Put your own flushes before `shutdown()` unless they fit inside the grace with it.
2. **The temporary database.** Where it lives (`tempfile.gettempdir()`, so `TMPDIR`), that a read-only root filesystem needs a writable `/tmp` (a `tmpfs` mount is enough), and that its size is bounded by `history_retention_seconds`.
3. **Logging.** Pass `logger=` or configure the `"Quiv"` logger; nothing is configured for you. Link to the getting-started Logging section.
4. **Restarts.** quiv starts empty; that is by design and the roadmap says why. The practice: re-add every task at startup from your own configuration; stagger the delays so a boot does not run everything at once; keep the time of a one-off alarm in your own store and re-add it with `run_at` at boot; keep a slow backstop poll while you build trust in the restore. Say which application each practice came from, in one line, so the reader knows it is field practice.
5. **A task that runs daily at a time.** The recipe: compute the next occurrence in UTC, pass it as `run_at` with `interval=86400`, and keep weekday rules inside the handler. Include the helper as it is written in ten-acre, `next_utc_time(hour, minute)`. Note that quiv schedules in UTC, so a local-time schedule computes the next occurrence in its zone and converts, and that this is not cron and will not become cron.
6. **Health.** `is_running`, `stats().loop_alive`, the 503 endpoint, and a Docker `HEALTHCHECK` line that calls it.
7. **Work handed to the main loop.** Link to the shutdown section of `run-on-main.md` and show the `pending_main_loop_work()` loop in the lifespan.
8. **One process, one pool.** `pool_size` is threads; CPU-bound work does not scale with it; point to Phase 7 for process jobs.

`docs/getting-started.md` Troubleshooting gains links to sections 1, 4, and 6. `docs/index.md` gains the page in its list. `docs/llms.txt` gains the page.

---

## 5. Other docs and artifacts

- `docs/api.md`: `is_running`, the `loop_alive` field, the `after_current` table, the `task_name` filter.
- `docs/observability.md`: the Health section (§1).
- `quiv/AGENTS.md`, `skills/quiv/SKILL.md`: the three names and the container rules of thumb (shutdown timeout under the stop grace; `is_running` for health; the daily recipe).
- `CLAUDE.md` key patterns: "Health" and the `rerun_requested` rule in the task lifecycle bullet.
- `docs/release-notes.md`: a `v1.4.0` entry.

All prose follows the global `orwell-writing` skill, one paragraph per line.

---

## 6. Tests

`tests/test_base.py`:

- `test_is_running_is_false_before_start_true_after_and_false_after_shutdown`.
- `test_stats_loop_alive_matches_is_running`.
- `test_is_running_is_false_when_the_loop_thread_died`: patch `_loop` to raise `BaseException` subclass `SystemExit` on entry (outside `Exception`, so the loop's own handler does not catch it), `start()`, poll until the thread is dead, assert `is_running` is false.

`tests/test_scheduler.py`:

- `test_run_task_immediately_after_current_runs_again_when_the_job_finishes`: interval 60, handler waits on an event; call with `after_current=True` while running; release; the second job starts within 1 s; `get_task().rerun_requested` is true between the call and the second start, false after.
- `test_run_task_immediately_after_current_on_a_run_once_task_raises`.
- `test_run_task_immediately_after_current_on_a_paused_task_raises`.
- `test_run_task_immediately_after_current_wins_over_retry_backoff`: a failing handler with `max_retries=1, retry_backoff=30`; the rerun happens at once, and `retry_attempt` is 1.
- `test_pause_task_clears_a_pending_rerun`: flag set, pause, resume with `delay=0`; exactly one job follows.
- `test_run_task_immediately_default_still_raises_on_a_running_task`.
- `test_get_all_tasks_filters_by_task_name` and `..._by_task_name_with_include_run_once`.

`tests/test_persistence.py`: the four outcomes of `queue_task_for_immediate_run` and the flag in `finalize_task_after_job`. `tests/test_models.py`: `rerun_requested` and `loop_alive` round-trip and default.

---

## Pitfalls

- **`is_running` must not touch the database.** A health probe runs every few seconds on every replica; keep it to two booleans and `is_alive()`.
- **`_started` is not `thread.is_alive()`.** Before `start()` the thread object exists and is not alive; after a crash it is not alive either. `is_running` needs both `_started` and `is_alive()` so "never started" and "died" both read false, and `_shutdown` so a stopped scheduler reads false while its thread finishes joining.
- **A field added to a frozen dataclass without a default breaks positional construction** anywhere it is built. `loop_alive` takes a default and goes last.
- **The rerun flag must be read before the branches in `finalize_task_after_job`,** because the retry branch returns early and would otherwise skip it. Apply it after `next_run_at` is computed so the flag wins.
- **Do not add jitter to a requested run.** The user asked for now.
- **`pause_task` must clear the flag,** or resume runs the task twice.
- **The container page is field practice, not policy.** Name the source application for each practice in one line; do not present ten-acre's five-minute backstop as a rule for everyone.
- **Docs-only commits deploy the site on push to `main`.** Shipping the page before the code is fine, but do not document `is_running` on it until the code exists; keep section 6 in the same commit as §1.

---

## Exit checklist

- [ ] `uv run pytest` green; coverage at or above the gate.
- [ ] `uv run mypy quiv` zero errors.
- [ ] Every test in §6 present and passing.
- [ ] `docs/containers.md` in the nav; `docs/index.md`, `docs/getting-started.md`, `docs/observability.md`, `docs/api.md` updated; `uv run zensical build --clean` clean.
- [ ] `scripts/soak.py` reads `scheduler.is_running` instead of `scheduler.thread.is_alive()`.
- [ ] `quiv/AGENTS.md`, `skills/quiv/SKILL.md`, `docs/llms.txt` updated; `claude plugin validate .` passes.
- [ ] `CLAUDE.md` updated.
- [ ] `docs/release-notes.md` entry; version bumped; `docs/roadmap.md` Phase 9 marked complete with the date.
