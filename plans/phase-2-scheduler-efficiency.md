# Phase 2 — Scheduler Core Efficiency (v0.6.0)

Two changes: (1) replace the fixed 1-second polling tick with an
interruptible sleep-until-next-due, (2) cache handler signature
introspection. Requires Phase 1 to be merged (this plan edits
`_dispatch_due_task` and `_loop` as they exist *after* Phase 1).

---

## Change 1: Smart sleep loop

### Current behavior

`Quiv._loop()` (`quiv/scheduler.py`) sleeps a hard `time.sleep(1)` every
iteration and re-queries the DB for due tasks. Consequences: up to ~1 s of
dispatch jitter, sub-second intervals impossible, and a DB query + lock
acquisition every second even when nothing is due for hours. History
cleanup is driven by a tick counter (`ticks_since_cleanup`).

### Design (prescriptive — do not deviate)

1. **Wake event.** In `QuivBase.__init__` (`quiv/base.py`), add:

   ```python
   self._wake_event = threading.Event()
   ```

   Add a tiny helper on `QuivBase`:

   ```python
   def _wake_loop(self) -> None:
       """Wake the scheduler loop early to re-evaluate due tasks."""
       self._wake_event.set()
   ```

2. **Next-due query.** Add to `PersistenceLayer`
   (`quiv/persistence.py`):

   ```python
   def get_next_due_time(self) -> datetime | None:
       """Return the earliest next_run_at among ACTIVE tasks, or None."""
       with self._lock, Session(self._engine) as session:
           statement = (
               select(TaskDB.next_run_at)
               .where(TaskDB.status == TaskStatus.ACTIVE)
               .order_by(col(TaskDB.next_run_at).asc())
               .limit(1)
           )
           value = session.exec(statement).first()
           # SQLite returns naive datetimes; normalize like model loads do.
           if value is not None and value.tzinfo is None:
               value = value.replace(tzinfo=timezone.utc)
           return value
   ```

   (Import `timezone` from `datetime` in persistence.py. Note: selecting
   a single column returns the raw datetime, bypassing the model
   reconstructor — hence the manual UTC normalization. Write a test for
   exactly this.)

3. **Rewrite `_loop`** (`quiv/scheduler.py`), replacing the tick counter
   with a wall-clock cleanup deadline:

   ```python
   def _loop(self) -> None:
       while not getattr(self, "_initialized", False):
           time.sleep(0.1)

       self._logger.info("Scheduler loop starting")
       CLEANUP_INTERVAL = 60.0
       MIN_SLEEP = 0.01   # floor: never busy-spin
       MAX_SLEEP = 60.0   # ceiling: bounded staleness safety net
       next_cleanup = time.monotonic()  # run cleanup on first iteration
       while not self._shutdown:
           try:
               if time.monotonic() >= next_cleanup:
                   self.persistence.cleanup_history(self.history_limit)
                   next_cleanup = time.monotonic() + CLEANUP_INTERVAL

               now = self._now_utc()
               if self._active_job_count < self._pool_size:
                   for task in self.persistence.get_due_tasks(now):
                       if self._active_job_count >= self._pool_size:
                           break
                       self._dispatch_due_task(task, now)

               sleep_for = self._compute_sleep_seconds(next_cleanup)
               self._wake_event.wait(timeout=sleep_for)
               self._wake_event.clear()
           except Exception as e:
               self._logger.error(f"Error in scheduler loop: {e}")
               self._wake_event.wait(timeout=5)
               self._wake_event.clear()
   ```

   And the helper:

   ```python
   def _compute_sleep_seconds(self, next_cleanup: float) -> float:
       """Seconds to sleep until the next scheduled wake-up."""
       candidates = [next_cleanup - time.monotonic()]
       next_due = self.persistence.get_next_due_time()
       if next_due is not None:
           candidates.append(
               (next_due - self._now_utc()).total_seconds()
           )
       return max(0.01, min(min(candidates), 60.0))
   ```

   (Use the same MIN/MAX literals; hoist them to module-level constants
   `_MIN_SLEEP_SECONDS` / `_MAX_SLEEP_SECONDS` / `_CLEANUP_INTERVAL_SECONDS`.)

4. **Wake calls.** Call `self._wake_loop()` at the end of (after the DB
   write, before returning):
   - `Quiv.add_task()`
   - `QuivBase.run_task_immediately()`
   - `QuivBase.resume_task()`
   - `QuivBase.shutdown()` — immediately after `self._shutdown = True`,
     so the loop exits without waiting out its current sleep. **This
     matters:** Phase 1's `thread.join(timeout=...)` otherwise waits up
     to 60 s.
   - `Quiv._run_job()` — in the `finally` block, right after the
     `_active_job_count` decrement. Rationale: when the pool was
     saturated, the loop skipped dispatching; a finishing job frees a
     slot, and deferred-due tasks must not wait for the next natural
     wake. Also, `finalize_task_after_job` just computed a new
     `next_run_at` that the sleeping loop doesn't know about.
   - `remove_task()` — optional but cheap; a removed task may have been
     the `min(next_run_at)`, and waking simply recomputes.

### Why the wait/clear ordering is race-free

- Wake fires **between dispatch and `wait()`**: the event is already set,
  so `wait()` returns immediately → loop re-queries. ✔
- Wake fires **between `wait()` returning and `clear()`**: we clear it,
  but the very next iteration re-queries due tasks anyway. ✔
- Never `clear()` before `wait()` in a different order than above — the
  pattern is exactly: `wait(timeout)`, then `clear()`, then loop back to
  the top which re-reads the DB.

### Tests (`tests/test_scheduler.py`)

- `test_get_next_due_time_returns_utc_aware`
  (`tests/test_persistence.py`): add two tasks with different delays,
  assert `get_next_due_time()` equals the earlier `next_run_at` and has
  `tzinfo` set; assert `None` when no tasks / all paused.
- `test_add_task_wakes_sleeping_loop`: start a scheduler with no tasks
  (loop will settle into a long sleep), then `add_task(..., run_once=True,
  interval=1, delay=0)` and assert a COMPLETED job appears within ~2 s
  (poll `get_all_jobs` with a deadline loop). Before this phase, that
  already worked within ~1 s; the real assertion is the *tightness*:
  record `t0` before `add_task` and assert job start latency < 0.5 s.
- `test_subsecond_interval_runs_multiple_times`: `interval=0.2`,
  `fixed_interval=False`, let it run ~1.5 s, assert ≥ 3 completed jobs.
- `test_idle_loop_does_not_poll_db`: monkeypatch/wrap
  `scheduler.persistence.get_due_tasks` with a counting wrapper, start
  the scheduler with **no** tasks, sleep 3 s, assert the count is small
  (≤ 2, i.e. startup iterations only — not ~3 as the old 1 Hz poll would
  give). Do the same for the saturation wake: not required, covered by
  the next test.
- `test_backpressure_deferred_task_dispatches_when_slot_frees`:
  `pool_size=1`; task A holds the slot for ~1 s; task B due immediately.
  Assert B's job starts within ~0.5 s of A finishing (compare B's
  `started_at` to A's `ended_at`), not up to a full sleep later.
- `test_shutdown_returns_promptly_when_loop_idle`: start scheduler with
  a task due far in the future (delay=3600), call `shutdown()` and assert
  it returns in well under 1 s.

### Pitfalls

- **Do not** compute the sleep *before* dispatching — a dispatch changes
  `next_run_at` (task goes RUNNING, drops out of the due set). Compute it
  last, immediately before `wait()`, as written above.
- `get_next_due_time` selects a bare column → **naive datetime** from
  SQLite. Forgetting the UTC normalization makes
  `(next_due - self._now_utc())` raise
  `TypeError: can't subtract offset-naive and offset-aware datetimes`,
  which the loop's catch-all turns into a silent 5 s crawl. The dedicated
  test above must exist.
- The error path in `_loop` must also use `self._wake_event.wait(5)` (not
  `time.sleep(5)`) so shutdown stays prompt even mid-error-backoff.
- Keep `time.monotonic()` for cleanup scheduling (immune to wall-clock
  jumps); keep `_now_utc()` for task due-times (they are wall-clock).
- The existing tests assume ~1 s dispatch latency generously; none should
  break, but if any asserted *minimum* latency, fix the test, not the
  loop.

---

## Change 2: Handler signature cache

### Current behavior

`ExecutionLayer.prepare_invocation()` (`quiv/execution.py`) calls
`self._accepts_keyword_arg(func, ...)` three times per dispatch
(`_job_id`, `_stop_event`, `_progress_hook`), and each call runs
`inspect.signature(func)` — reflection on every single job dispatch.

### Design

1. Add to `ExecutionLayer.__init__`:

   ```python
   import weakref
   self._injectable_cache: (
       "weakref.WeakKeyDictionary[Callable[..., Any], frozenset[str]]"
   ) = weakref.WeakKeyDictionary()
   ```

   `WeakKeyDictionary` (not a plain dict) so cached entries die with the
   handler — the registry drops handlers on `remove_task`/run-once
   completion, and a plain dict would leak them for the scheduler's
   lifetime.

2. Replace the three `_accepts_keyword_arg` calls with one lookup:

   ```python
   _INJECTABLE_KWARGS = frozenset({"_job_id", "_stop_event", "_progress_hook"})

   def _get_injectable_params(self, func: Callable[..., Any]) -> frozenset[str]:
       """Return which injectable kwargs this callable accepts (cached)."""
       try:
           cached = self._injectable_cache.get(func)
       except TypeError:          # unhashable callable — compute uncached
           cached = None
       if cached is not None:
           return cached
       accepted = self._compute_injectable_params(func)
       try:
           self._injectable_cache[func] = accepted
       except TypeError:          # unhashable or not weakref-able
           pass
       return accepted

   def _compute_injectable_params(self, func: Callable[..., Any]) -> frozenset[str]:
       try:
           signature = inspect.signature(func)
       except (ValueError, TypeError):
           return frozenset()
       accepted = set()
       for parameter in signature.parameters.values():
           if parameter.kind == parameter.VAR_KEYWORD:
               return _INJECTABLE_KWARGS  # **kwargs accepts everything
           if parameter.name in _INJECTABLE_KWARGS:
               accepted.add(parameter.name)
       return frozenset(accepted)
   ```

   Then in `prepare_invocation`:

   ```python
   injectable = self._get_injectable_params(func)
   if "_job_id" in injectable:
       f_kwargs["_job_id"] = job_id
   if "_stop_event" in injectable:
       f_kwargs["_stop_event"] = stop_event
   if "_progress_hook" in injectable:
       ...  # unchanged closure
   ```

3. Keep `_accepts_keyword_arg` **only if** something else uses it (grep
   first); otherwise delete it and migrate its tests in
   `tests/test_execution.py` to `_compute_injectable_params`.

### Tests (`tests/test_execution.py`)

- `test_injectable_params_cached`: wrap `inspect.signature` with a
  counting monkeypatch, call `prepare_invocation` for the same handler
  3×, assert `inspect.signature` ran once.
- `test_injectable_params_var_keyword`: handler with `**kwargs` gets all
  three injected.
- `test_injectable_params_unhashable_callable`: a callable whose class
  defines `__eq__` without `__hash__` (unhashable instance with
  `__call__`) still dispatches correctly, computed per-call.
- `test_cache_does_not_leak_removed_handlers`: register a handler as a
  local function, run `prepare_invocation`, take a `weakref.ref` to the
  function, delete all strong refs (including the scheduler registry
  entry), `gc.collect()`, assert the weakref is dead. (This proves
  `WeakKeyDictionary` is doing its job.)
- Existing injection tests (`test_job_id_injected_into_handler`, the
  `_stop_event`/`_progress_hook` tests) must pass unchanged.

### Pitfalls

- **Some callables are not weakref-able** (builtins, some C extensions)
  and some are unhashable — both `self._injectable_cache[func] = ...`
  and `.get(func)` can raise `TypeError`. Both sites are guarded above;
  keep both guards.
- Two distinct handlers with equal `__eq__` could collide in a normal
  dict; function objects use identity equality, so this is a non-issue
  for plain functions — but `functools.partial` objects and bound
  methods create a **new object per access** (`obj.method` is a fresh
  bound method each time). That means bound methods may never hit the
  cache (each registration is one object — fine, the registry holds one
  stable reference and `prepare_invocation` receives that same object).
- Do not cache on `id(func)` — ids get reused after GC and would return
  a stale signature set for a different function.

---

## Exit checklist

- [ ] `uv run pytest` green; new tests listed above all present.
- [ ] `uv run mypy quiv` — zero errors (strict). WeakKeyDictionary
      needs the quoted generic annotation as written.
- [ ] Manual check: `uv run python -c` snippet — start a scheduler with a
      task at `interval=0.1`, confirm ~10 jobs/second, shutdown returns
      instantly.
- [ ] `docs/api.md` / `docs/architecture.md`: update the "polls every
      second" wording — the loop now sleeps until the next due task and
      wakes on schedule changes. `CLAUDE.md` has the same wording in
      *Architecture* → update it too.
- [ ] `docs/release-notes.md` v0.6.0 section: sub-second intervals now
      supported; idle scheduler no longer polls.
- [ ] Version `0.6.0` in `pyproject.toml`; roadmap page updated.
