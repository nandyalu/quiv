# Phase 4 — Execution Features (v0.6.0)

Three features shipping together because they all touch the `add_task()`
signature, the `TaskDB`/`Task` models, and the job-finalize path: per-task
**timeout**, **retry/backoff**, and **jitter**. Requires Phases 1–3 merged
(timeout enforcement rides on Phase 2's wake-event loop).

**No DB migration machinery is needed**: every `Quiv` instance creates a
fresh temp SQLite file at init, so new columns simply exist on next start.
Do not add Alembic or version tables.

---

## 1. Model & API surface changes (do these first, all features share them)

### `quiv/models.py`

`TaskDB` gains columns (with defaults, so all existing tests construct
fine):

```python
timeout_seconds: float | None = None
max_retries: int = 0
retry_backoff_seconds: float = 30.0
retry_attempt: int = 0          # consecutive-failure counter, internal
jitter_seconds: float = 0.0
```

`Task` (the public pydantic model) gains the same five fields (mirror the
defaults; `retry_attempt` is included — it is useful observability). Add
them to the `_convert_from_task_db` extraction dict too — that validator
builds the dict **by hand**; forgetting a field there silently drops it
(pitfall — write a test asserting `get_task()` round-trips all new
fields).

`Job` gains:

```python
attempt: int = 1                # 1 = first try, 2 = first retry, ...
```

`Event` gains:

```python
JOB_RETRYING = "job_retrying"
```

with a docstring line matching the existing attribute-doc style.

### `Quiv.add_task()` (`quiv/scheduler.py`)

New keyword parameters, all optional, inserted after `fixed_interval`:

```python
timeout: float | None = None,
max_retries: int = 0,
retry_backoff: float = 30.0,
jitter: float = 0.0,
```

Validation (raise `ConfigurationError`, matching existing messages'
style):

- `timeout is not None and timeout <= 0` → "timeout must be greater than 0"
- `max_retries < 0` → "max_retries must be greater than or equal to 0"
- `retry_backoff <= 0` → "retry_backoff must be greater than 0"
- `jitter < 0` → "jitter must be greater than or equal to 0"

Pass all four through `persistence.create_task(...)` (extend its
signature and the `TaskDB(...)` construction).

---

## 2. Per-task timeout

### Semantics (document verbatim in docstring + docs)

Timeout is **cooperative**: when a job exceeds `timeout` seconds, quiv
sets its stop event — exactly as `cancel_job()` would. A handler that
checks `_stop_event` exits promptly and the job finalizes as `CANCELLED`
with `error_message = "Job exceeded timeout of {timeout}s"`. A handler
that ignores its stop event keeps occupying its pool thread (quiv never
kills threads); it still finalizes as CANCELLED when it eventually
returns. No new `JobStatus` member — timeouts are cancellations with an
error message.

### Implementation

1. `QuivBase.__init__` gains:

   ```python
   self._job_deadlines: dict[str, float] = {}   # job_id -> monotonic deadline
   self._timed_out_jobs: set[str] = set()
   ```

   Both protected by the existing `_registries_lock` from Phase 1 (do not
   add another lock; contention is negligible).

2. `_dispatch_due_task` (`quiv/scheduler.py`): after registering the stop
   event, if `task.timeout_seconds` is not None:

   ```python
   with self._registries_lock:
       self._job_deadlines[job_id] = (
           time.monotonic() + task.timeout_seconds
       )
   ```

3. New method on `Quiv`, called from `_loop` right before the
   due-task dispatch block (i.e. every wake):

   ```python
   def _enforce_timeouts(self) -> None:
       """Set stop events for jobs past their deadline."""
       now = time.monotonic()
       with self._registries_lock:
           expired = [
               job_id for job_id, deadline in self._job_deadlines.items()
               if now >= deadline
           ]
           for job_id in expired:
               del self._job_deadlines[job_id]
               self._timed_out_jobs.add(job_id)
       for job_id in expired:
           if self.cancel_job(job_id):
               self._logger.warning(
                   f"Job {job_id} exceeded its timeout; stop event set."
               )
   ```

4. `_compute_sleep_seconds` (Phase 2) gains a third candidate: the
   soonest deadline remaining —
   `min(self._job_deadlines.values()) - time.monotonic()` when non-empty
   (snapshot under the lock). Otherwise a timeout could fire up to 60 s
   late while the loop sleeps.

5. `_run_job` finally block: clean up and label —

   ```python
   with self._registries_lock:
       self._job_deadlines.pop(job_id, None)
       timed_out = job_id in self._timed_out_jobs
       self._timed_out_jobs.discard(job_id)
   ```

   When `timed_out` and the final status is `CANCELLED` and
   `error_message` is None, set
   `error_message = f"Job exceeded timeout of {timeout}s"` — pass the
   task's `timeout_seconds` into `_run_job` (add a parameter; the
   `task_snapshot` already carries it, use `task_snapshot.timeout_seconds`
   instead of a new parameter).

### Tests (`tests/test_scheduler.py`)

- `test_timeout_cancels_cooperative_handler`: handler loops on
  `_stop_event.wait(0.05)` for up to 10 s; `timeout=0.5`. Poll until the
  job is terminal; assert status `cancelled`, `error_message` mentions
  "timeout", and total wall time < ~3 s.
- `test_timeout_fires_promptly_during_long_loop_sleep`: only one task,
  no other schedule activity (so the loop would otherwise sleep long);
  assert cancellation latency ≈ timeout ± 0.5 s. This is the
  `_compute_sleep_seconds` regression test.
- `test_no_timeout_means_no_deadline`: task without timeout →
  `scheduler._job_deadlines` stays empty during its run.
- `test_timeout_ignoring_handler_still_finalizes_cancelled`: handler
  without `_stop_event` param sleeping 1 s, `timeout=0.2` — job ends
  `cancelled` (the `_run_job` stop-event check catches it) with the
  timeout error message.

---

## 3. Retry / backoff

### Semantics (document verbatim)

Applies to **FAILED** jobs only (an exception escaped the handler).
CANCELLED jobs — including timeouts — never retry (cancellation is
deliberate). On failure with retries remaining, the next run is scheduled
at `now + retry_backoff * 2**(failures_so_far - 1)` (exponential: first
retry after `retry_backoff`, second after `2×`, then `4×`, ...). A
successful run resets the failure counter. When retries are exhausted:
recurring tasks fall back to their normal interval schedule (counter
resets); run-once tasks are deleted. `Event.JOB_RETRYING` fires (after
`JOB_FAILED`) whenever a retry has been scheduled, with the usual
`(event, task, job)` payload.

### Implementation

1. `PersistenceLayer.finalize_task_after_job` — new signature:

   ```python
   def finalize_task_after_job(
       self, task_id: str, job_started_at: datetime, job_failed: bool
   ) -> bool:
       """... Returns True when a retry was scheduled."""
   ```

   New logic (inside the existing session/lock, replacing the body after
   the `existing is None` check):

   ```python
   now = self._now_utc()
   if job_failed and existing.retry_attempt < existing.max_retries:
       existing.retry_attempt += 1
       backoff = existing.retry_backoff_seconds * (
           2 ** (existing.retry_attempt - 1)
       )
       existing.status = TaskStatus.ACTIVE
       existing.next_run_at = now + timedelta(seconds=backoff)
       session.commit()
       return True

   existing.retry_attempt = 0
   if existing.run_once:
       session.delete(existing)
       session.commit()
       return False
   existing.status = TaskStatus.ACTIVE
   # ... existing fixed_interval / non-fixed next_run computation ...
   session.commit()
   return False
   ```

2. `_run_job` (`quiv/scheduler.py`):
   - Compute `job_failed = status == JobStatus.FAILED` (after the
     stop-event CANCELLED override — order matters: a job that both
     raised *and* was cancelled counts as cancelled, no retry).
   - `will_retry = self.persistence.finalize_task_after_job(task_id,
     start_time, job_failed)`.
   - Run-once registry cleanup becomes conditional:
     `if run_once and not will_retry:` (otherwise the handler for the
     retry is gone → the Phase 1 guard would skip the retry dispatch).
   - After the existing event emission, add:
     `if will_retry: self._emit_event(Event.JOB_RETRYING, task_snapshot,
     finalized_job)`.
   - The `_wake_loop()` call from Phase 2 already ensures the loop
     notices the (possibly very near) retry time.

3. `_dispatch_due_task`: set the job's attempt number —
   `create_job(task.id, task.task_name, attempt=task.retry_attempt + 1)`;
   extend `PersistenceLayer.create_job` accordingly.

### Tests

- `test_failed_job_retries_then_succeeds`: handler fails on calls 1–2
  (count via a list closure), succeeds on call 3; `max_retries=3`,
  `retry_backoff=0.1` *(validation allows small positive floats — keep
  tests fast)*, `run_once=True`. Assert: eventually one COMPLETED job;
  exactly 2 FAILED jobs; job `attempt` fields are 1, 2, 3; task row
  deleted at the end.
- `test_retries_exhausted_run_once_deletes_task`: always-failing handler,
  `max_retries=1`, `run_once=True` → 2 FAILED jobs then task gone,
  `TaskNotFoundError` from `get_task`.
- `test_retries_exhausted_recurring_returns_to_schedule`: always-failing,
  `max_retries=1`, `interval=0.3` → after exhaustion the task still
  exists, status `active`, `retry_attempt == 0`.
- `test_job_retrying_event_emitted`: listener on `Event.JOB_RETRYING`
  records payloads; assert fired once per scheduled retry with a `Job`
  whose status is `failed`.
- `test_cancelled_job_does_not_retry`: cooperative handler +
  `cancel_job()` mid-run, `max_retries=3` → no retry scheduled
  (`retry_attempt` stays 0, next_run_at follows the normal interval).
- `test_backoff_delay_grows`: `retry_backoff=0.2, max_retries=2`; capture
  `next_run_at` after each failure (poll `get_task`); assert gaps ≈ 0.2
  then ≈ 0.4 (generous tolerance, e.g. ±0.15).

### Pitfalls

- The **cancelled-overrides-failed ordering** in `_run_job`'s finally:
  compute `job_failed` *after* the stop-event check flips status to
  CANCELLED, or a cancelled-while-raising job would retry.
- `finalize_task_after_job` is also reached when the task row was
  deleted mid-run (`existing is None` → return `False`), and Phase 1's
  dispatch guard skips unregistered handlers — both paths must stay.
- Do not emit JOB_RETRYING *instead of* JOB_FAILED — both fire; tests
  and docs say so.
- Keep `2 ** (attempt - 1)` in Python ints/floats — no overflow risk at
  sane retry counts, but validation already bounds nothing; mention in
  docs that large `max_retries` × exponential backoff grows fast.

---

## 4. Jitter

### Semantics

`jitter=J` adds `uniform(0, J)` seconds to each computed **recurring**
next-run time (both `fixed_interval` modes). Purpose: de-synchronize many
tasks aligned to the same boundaries (thundering herd). Not applied to
the initial `delay` (caller controls that directly) nor to retry backoff
(retries stay deterministic).

### Implementation

In `finalize_task_after_job`, after the normal `next_run_at` computation
(both branches), add:

```python
if existing.jitter_seconds > 0:
    existing.next_run_at += timedelta(
        seconds=random.uniform(0, existing.jitter_seconds)
    )
```

Import `random` at the top of `persistence.py`.

### Tests

- `test_jitter_offsets_next_run` (`tests/test_persistence.py`): create a
  task with `interval=100, jitter=5, fixed_interval=True` directly via
  persistence; call `finalize_task_after_job(task_id, started_at,
  job_failed=False)`; assert `next_run_at` lies in
  `(boundary, boundary + 5]`-ish — deterministically: patch
  `random.uniform` (monkeypatch `quiv.persistence.random.uniform`) to
  return a fixed value and assert exact arithmetic.
- `test_zero_jitter_unchanged`: default task → next_run_at exactly on the
  boundary (existing behavior; an existing test may already cover it —
  extend rather than duplicate).

### Pitfalls

- Patch `random.uniform` via the **`quiv.persistence`** module namespace,
  not the global `random` module, so the test stays hermetic.
- Jitter is re-rolled every run (fresh `uniform` per finalize) — do not
  cache a per-task offset; the point is decorrelation over time.

---

## Exit checklist

- [ ] `uv run pytest` green — including all ~12 new tests above.
- [ ] `uv run mypy quiv` — zero errors.
- [ ] `Task` round-trip test proves all five new fields survive
      `get_task()` / `get_all_tasks()`.
- [ ] New docs page `docs/failure-handling.md` (timeout semantics, retry
      semantics, jitter) added to `zensical.toml` nav (before "Exceptions"
      is a sensible spot) and `uv run zensical build --clean` passes.
- [ ] `docs/api.md` updated for the new `add_task` params;
      `docs/event-listeners.md` documents `JOB_RETRYING`.
- [ ] `CLAUDE.md`: events list gains `JOB_RETRYING`; task-lifecycle
      bullet mentions retry/jitter; add a "timeouts are cooperative
      cancellations" line.
- [ ] Version `0.6.0`; roadmap page updated.
