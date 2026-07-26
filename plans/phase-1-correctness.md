# Phase 1 — Correctness & Stability (v0.5.0)

Three independent bug fixes. Implement and test them one at a time, in the
order below. No new features; the only public-API change is a new optional
`timeout` parameter on `shutdown()` and a new exception class.

---

## Fix 1: Double-run guard in `run_task_immediately`

### Bug

`PersistenceLayer.queue_task_for_immediate_run()` (`quiv/persistence.py`,
~line 167) unconditionally sets `task.status = TaskStatus.ACTIVE` and
`task.next_run_at = now`. Two broken cases:

- Task is `RUNNING`: flipping it to `ACTIVE` makes the scheduler loop
  dispatch a **second concurrent job** for the same task, breaking the
  no-overlap invariant (`mark_task_running` exists precisely to prevent
  this).
- Task is `PAUSED`: the call silently un-pauses it. Un-pausing must stay
  an explicit `resume_task()` action.

### Change

1. Add a new exception in `quiv/exceptions.py`, following the existing
   style there (all inherit from `QuivError`):

   ```python
   class TaskNotActiveError(QuivError):
       """Raised when an operation requires an ACTIVE task."""
   ```

2. Export it: add `TaskNotActiveError` to the imports and `__all__` in
   `quiv/__init__.py`.

3. In `queue_task_for_immediate_run()`, after the existing `None` check,
   add:

   ```python
   if task.status != TaskStatus.ACTIVE:
       raise TaskNotActiveError(
           f"Task '{task_id}' is {task.status}; only active tasks can be"
           " queued for immediate run. Use resume_task() for paused tasks."
       )
   ```

   Then keep the existing `next_run_at = now` assignment. The
   `task.status = TaskStatus.ACTIVE` line becomes redundant — remove it.

4. Update the docstrings of both `queue_task_for_immediate_run()` and
   `QuivBase.run_task_immediately()` (`quiv/base.py`, ~line 511) to
   document the new `TaskNotActiveError`.

### Tests (add to `tests/test_persistence.py` and/or `tests/test_scheduler.py`)

- `test_run_task_immediately_rejects_running_task` — create a scheduler
  (do **not** start it), `add_task(...)`, call
  `scheduler.persistence.mark_task_running(task_id)`, then assert
  `pytest.raises(TaskNotActiveError)` on
  `scheduler.run_task_immediately(task_id)`.
- `test_run_task_immediately_rejects_paused_task` — same shape but with
  `pause_task(task_id)`; also assert the task status is still `"paused"`
  afterwards (the un-pause bug).
- `test_run_task_immediately_active_task_still_works` — guard against
  regression of the happy path (there is an existing test
  `test_add_task_and_run_task_immediately_queues_task`; make sure it still
  passes).

### Pitfalls

- `TaskStatus` values are `str` enums; comparisons like
  `task.status != TaskStatus.ACTIVE` work because the column stores the
  string value — do not compare with `is`.
- Do not raise for the `None` case with the new exception — the existing
  `TaskNotScheduledError` for missing tasks must stay as-is (tests depend
  on it).

---

## Fix 2: Registry race in `_dispatch_due_task` (TOCTOU)

### Bug

`Quiv._dispatch_due_task()` (`quiv/scheduler.py`, ~line 207) runs on the
scheduler thread; `remove_task()` runs on whatever thread the application
calls it from (e.g. a FastAPI worker). Between `get_due_tasks()` returning
a task and the dispatch completing, `remove_task()` may have:

1. popped `self.registry[task.id]` → `func = self.registry[task.id]`
   raises `KeyError`;
2. deleted the task row → `mark_task_running()` / `get_task()` raise
   `TaskNotFoundError`.

Either exception is swallowed by the catch-all in `_loop()`, which then
sleeps **5 seconds**, stalling all other due tasks.

### Change (in `_dispatch_due_task`)

Reorder and guard. The current order is: `create_job` → registry lookup →
`prepare_invocation` → `mark_task_running` → snapshot → submit. New order:

```python
def _dispatch_due_task(self, task: TaskDB, now: datetime) -> None:
    func = self.registry.get(task.id)
    if func is None:
        # Task was removed between the due-query and dispatch.
        self._logger.warning(
            f"Skipping dispatch for task '{task.id}': handler no longer"
            " registered (task was likely removed)."
        )
        return

    try:
        self.persistence.mark_task_running(task.id)
        task_snapshot = self.get_task(task.id)
    except TaskNotFoundError:
        self._logger.warning(
            f"Skipping dispatch for task '{task.id}': task row was"
            " deleted before dispatch."
        )
        return

    job_id = self.persistence.create_job(task.id, task.task_name)
    stop_event = threading.Event()
    self.stop_events[job_id] = stop_event

    f_args, f_kwargs = self.execution.prepare_invocation(...)  # unchanged
    self._logger.info(...)  # unchanged

    with self._job_count_lock:
        self._active_job_count += 1
    self.executor.submit(self._run_job, ...)  # unchanged
```

Key points:

- **Registry lookup first**, before any DB writes, using `.get()`.
- `mark_task_running` + snapshot happen **before** `create_job`, wrapped
  in `try/except TaskNotFoundError`, so no orphan job row is created when
  the task vanished.
- Import `TaskNotFoundError` from `.exceptions` in `scheduler.py`.
- `create_job` has a `foreign_key` to `quiv_task.id`; creating the job
  after `mark_task_running` succeeded closes the window where the FK
  target is gone. (SQLite does not enforce FKs here by default, but do it
  anyway for correctness of the history.)

There is still a tiny window where `remove_task()` deletes the row after
`mark_task_running` succeeds — that is fine: `remove_task` sets the stop
event for running jobs, `_run_job`'s `finalize_task_after_job` already
tolerates a missing row (returns silently), and the job finalizes as
cancelled/completed normally.

### Also: protect shared dict mutations

`registry`, `progress_callbacks`, and `stop_events` are plain dicts
mutated from multiple threads. Single-key reads/writes are atomic under
the GIL, but make the intent explicit and future-proof:

- Add `self._registries_lock = threading.Lock()` in `QuivBase.__init__`.
- Hold it in: `_register_handler`, `_register_progress_callback`, the
  `.pop()` calls in `remove_task()` and `_run_job()`'s finally block, and
  the `.get()` in `_dispatch_due_task`.
- Do **not** hold it while calling handlers or DB methods — lock only the
  dict operation itself (keep critical sections to one or two lines).

### Tests

- `test_remove_task_racing_dispatch_does_not_stall_loop`
  (`tests/test_scheduler.py`): hard to hit the race deterministically, so
  test the guard directly — create a scheduler (not started), `add_task`,
  then `scheduler.registry.pop(task_id)`, fetch the `TaskDB` row via
  `scheduler.persistence.get_task(task_id)`, and call
  `scheduler._dispatch_due_task(row, scheduler._now_utc())` directly.
  Assert: no exception, no job rows created
  (`scheduler.get_all_jobs() == []`), task status unchanged.
- `test_dispatch_skips_when_task_row_deleted`: same shape, but delete the
  row (`scheduler.persistence.delete_task(task_id)`) while leaving the
  registry entry, then call `_dispatch_due_task` with the previously
  fetched row object. Assert no exception and no job created.

### Pitfalls

- The fetched `TaskDB` row object is detached after its session closes;
  read its attributes (`task.id`, `task.task_name`, `task.args`, ...)
  — do not lazy-load relationships (there are none — keep it that way).
- Do not wrap the whole `_dispatch_due_task` body in one try/except that
  hides real bugs — only catch `TaskNotFoundError`, and only around the
  two calls that can legitimately race.
- `_active_job_count` must only be incremented when
  `executor.submit(...)` is actually reached; the early returns above must
  not touch it (otherwise the count leaks and backpressure jams).

---

## Fix 3: Shutdown hardening

### Bug

`QuivBase.shutdown()` (`quiv/base.py`, ~line 553):

1. Calls `self.get_all_jobs()` with **no status filter** — iterates the
   entire retained job history (potentially thousands of rows) just to
   set stop events; `cancel_job()` on finished jobs is a no-op anyway.
2. `self.thread.join()` and `self.executor.shutdown(wait=True)` have no
   timeout — one handler that ignores its stop event blocks application
   shutdown forever (painful in FastAPI lifespan teardown).

### Change

New signature: `def shutdown(self, timeout: float | None = None) -> None:`
(keep `stop()` as an alias, forwarding the argument).

```python
def shutdown(self, timeout: float | None = None) -> None:
    from .context import _unregister_active

    _unregister_active(self)
    self._shutdown = True

    # Signal cancellation to RUNNING jobs only.
    for job in self.get_all_jobs(status=JobStatus.RUNNING):
        if job.id is not None:
            self.cancel_job(job.id)

    if self.thread.is_alive():
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            self._logger.warning(
                "Scheduler loop did not stop within the shutdown timeout."
            )

    if timeout is None:
        self.executor.shutdown(wait=True)
    else:
        # Wait up to `timeout` for in-flight jobs to drain cooperatively.
        deadline = time.monotonic() + timeout
        while self._active_job_count > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._active_job_count > 0:
            undrained = [
                j.id for j in self.get_all_jobs(status=JobStatus.RUNNING)
            ]
            self._logger.warning(
                f"{self._active_job_count} job(s) still running after"
                f" {timeout}s shutdown timeout: {undrained}."
                " Abandoning them (threads are daemonic)."
            )
        self.executor.shutdown(wait=False, cancel_futures=True)

    # DB cleanup: unchanged (already wrapped in try/except).
    ...
```

Details:

- Import `time` at the top of `base.py`; import `JobStatus` from
  `.models` (check — `Job` is already imported there; add `JobStatus`).
- Docstring: document that with a timeout, undrained jobs are abandoned
  on daemon threads and may log errors afterwards (e.g. writing to the
  now-deleted DB); without a timeout, behavior is exactly as before.
- Keep the DB-cleanup `try/except` — abandoned workers may still touch
  the engine; on Linux, deleting an open SQLite file is safe (unlinked,
  reclaimed on close). This is a documented, accepted limitation.

### Tests

- `test_shutdown_only_cancels_running_jobs`
  (`tests/test_base.py`): run a run-once task to completion (existing
  patterns in `test_scheduler.py` show how to wait for a COMPLETED job),
  then start a second long-running task whose handler loops on
  `_stop_event`. Call `shutdown()` and assert it returns; assert the
  long task's job ended as `cancelled` and the completed job is untouched.
- `test_shutdown_timeout_with_hung_handler`: handler is
  `def hung(): time.sleep(30)` (ignores stop events). Start it, wait for
  its job to be RUNNING, call `shutdown(timeout=2.0)` and assert wall
  time < ~5 s (`time.monotonic()` around the call). Do NOT assert on the
  hung job's final status — it is abandoned.
- `test_shutdown_no_timeout_unchanged`: existing shutdown tests must all
  still pass unmodified.

### Pitfalls

- `executor.shutdown(cancel_futures=True)` only cancels **queued**
  futures, not running ones — that is expected; running ones are
  abandoned (daemon threads).
- The hung-handler test leaves a live thread behind for up to 30 s; keep
  the sleep short-ish (e.g. 30 s max) so the test process still exits
  promptly, and do not reuse that scheduler instance afterwards.
- Do not call `thread.join()` from within the scheduler thread itself —
  not currently possible, but do not add shutdown calls inside handlers
  in tests without expecting this.

---

## Exit checklist

- [ ] `uv run pytest` — all green, including the 7+ new tests above.
- [ ] `uv run mypy quiv` — zero errors (strict).
- [ ] `TaskNotActiveError` exported from `quiv` and documented in
      `docs/exceptions.md`.
- [ ] `docs/api.md` updated: `shutdown(timeout=...)`,
      `run_task_immediately` raises documented.
- [ ] `docs/release-notes.md` gains a `v0.5.0` section listing the three
      fixes (call out `TaskNotActiveError` as a behavior change:
      previously `run_task_immediately` silently un-paused tasks).
- [ ] Version bumped to `0.5.0` in `pyproject.toml`.
- [ ] `docs/roadmap.md` Phase 1 marked complete.
