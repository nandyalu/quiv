# Phase 5 — Management & Observability API (v0.7.0)

Three additive features: `update_task()`, rich job/task queries, and
`stats()`. Requires Phases 1–4 merged (update_task mutates fields
introduced in Phase 4).

---

## 1. `update_task()`

### Semantics (document verbatim)

Mutates a scheduled task in place, preserving its `task_id`. Only the
parameters you pass change; everything else is untouched. If `interval`
is changed, the next run is rescheduled to `now + interval` (documented,
deliberate simplification). Updating a `RUNNING` task is allowed — the
changes take effect from the next run. Emits `Event.TASK_UPDATED` with
the post-update `Task`. Raises `TaskNotFoundError` for unknown ids and
`ConfigurationError` for invalid values (same rules as `add_task`).

### The UNSET sentinel

`None` is a meaningful value for several fields (`timeout=None` disables
the timeout; `progress_callback=None` clears it), so "not passed" needs a
distinct sentinel. At module level in `quiv/scheduler.py`:

```python
class _Unset:
    """Sentinel for update_task parameters that were not passed."""
    def __repr__(self) -> str:  # pragma: no cover
        return "<UNSET>"

_UNSET = _Unset()
```

(A class instance, not `object()`, so mypy strict can type parameters as
`float | None | _Unset` etc. and `repr` reads well in logs.)

### Signature (on `Quiv`)

```python
def update_task(
    self,
    task_id: str,
    *,
    task_name: str | _Unset = _UNSET,
    interval: float | _Unset = _UNSET,
    fixed_interval: bool | _Unset = _UNSET,
    args: tuple[Any, ...] | _Unset = _UNSET,
    kwargs: dict[str, Any] | _Unset = _UNSET,
    timeout: float | None | _Unset = _UNSET,
    max_retries: int | _Unset = _UNSET,
    retry_backoff: float | _Unset = _UNSET,
    jitter: float | _Unset = _UNSET,
    progress_callback: Callable[..., Any] | None | _Unset = _UNSET,
) -> Task:
```

Not updatable (documented): `run_once`, `delay` (initial delay is a
creation-time concept), and the handler `func` (remove + re-add for a
new handler; changing the callable under a running job invites confusion).

### Implementation

1. Validate every provided value with **the same checks as `add_task`**
   — extract those checks into module-level helpers
   (`_validate_interval(value)`, `_validate_timeout(value)`, ...) used by
   both methods, rather than duplicating message strings.
2. Pickle `args`/`kwargs` when provided (reuse the try/except →
   `ConfigurationError` pattern from `add_task`; extract a
   `_pickle_or_raise(value, label)` helper).
3. New `PersistenceLayer.update_task(task_id, **column_updates) -> None`:
   holds the write lock, `session.get(TaskDB, task_id)` (raise
   `TaskNotFoundError` if None), `setattr` each provided column, and —
   when `interval_seconds` is among the updates —
   `next_run_at = self._now_utc() + timedelta(seconds=new_interval)`.
   Pass only concrete column values into this method (scheduler layer
   resolves sentinels; persistence never sees `_UNSET`).
4. `progress_callback` handling stays in the scheduler layer:
   `if not isinstance(progress_callback, _Unset):
   self._register_progress_callback(task_id, progress_callback)`
   (that method already treats `None` as "clear").
5. After the DB update: `self._wake_loop()` (the new schedule may be
   sooner than the loop's current sleep), fetch the fresh `Task`, emit
   `Event.TASK_UPDATED`, log, and return the `Task`.
6. `Event` enum gains `TASK_UPDATED = "task_updated"`; it is a `TASK_*`
   event → payload `(event, task)`.

### Tests (`tests/test_scheduler.py`)

- `test_update_task_changes_interval_and_reschedules`: add with
  `interval=100`, update to `interval=0.2` on a started scheduler, assert
  a job completes within ~1 s (proves both the reschedule and the wake).
- `test_update_task_partial_leaves_other_fields`: update only `jitter`;
  assert `get_task()` shows old interval/args/name intact.
- `test_update_task_clears_timeout_with_none`: create with
  `timeout=5`, update `timeout=None`, assert `timeout_seconds is None` —
  the sentinel-vs-None distinction test.
- `test_update_task_swaps_progress_callback` and
  `test_update_task_clears_progress_callback_with_none`.
- `test_update_task_validates_like_add_task`: `interval=0`,
  `max_retries=-1`, `args=[1]` (list) each raise `ConfigurationError`.
- `test_update_task_unknown_id_raises` → `TaskNotFoundError`.
- `test_task_updated_event_emitted` with the updated values visible on
  the payload `Task`.

### Pitfalls

- **Do not** let `_UNSET` leak into the persistence layer or the DB —
  resolve sentinels entirely in `update_task` before calling
  `persistence.update_task`.
- mypy strict: the `float | _Unset` unions require
  `isinstance(x, _Unset)` narrowing (not `x is _UNSET`) — `is` does not
  narrow custom sentinel unions for mypy. Use `isinstance` everywhere.
- Emitting `TASK_UPDATED` must use the **post-update** task (fetch after
  commit), not a stale snapshot.

---

## 2. Rich job/task queries

### `PersistenceLayer.get_all_jobs` — new signature

```python
def get_all_jobs(
    self,
    status: str | None = None,
    task_id: str | None = None,
    since: datetime | None = None,      # started_at >= since
    until: datetime | None = None,      # started_at <= until
    order_by: str = "started_at",       # "started_at" | "ended_at"
    descending: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> list[Job]:
```

- Whitelist `order_by`: `_JOB_ORDER_COLUMNS = {"started_at": Job.started_at,
  "ended_at": Job.ended_at}` module-level dict; unknown key →
  `ConfigurationError` listing valid options. **Never** `getattr(Job,
  order_by)` on raw input.
- Build the statement incrementally like the existing `status` filter;
  use `col(...)` wrappers for typed comparisons (existing convention in
  this file).
- `offset` without `limit` is valid SQL in SQLite via
  `.offset(offset)` — apply `.limit(limit)` only when not None, apply
  `.offset(offset)` when > 0.
- Mirror the signature on `QuivBase.get_all_jobs` (pure passthrough,
  docstring updated). **Backward compatible**: the only pre-existing
  parameter is `status`, first position — keep it first.

### `get_all_tasks` — add

```python
status: str | None = None, limit: int | None = None, offset: int = 0
```

after the existing `include_run_once` (again passthrough in `QuivBase`).
Order by `next_run_at` ascending (fixed, no `order_by` param — YAGNI).

### Tests (`tests/test_persistence.py`)

Seed one scheduler (not started) with jobs created directly through
`persistence.create_job` + `finalize_job` at controlled times (inject a
fake `now_utc` — `PersistenceLayer` takes `now_utc` in its constructor,
so build a `PersistenceLayer(engine, fake_now)` around the same engine,
stepping `fake_now` between inserts):

- filter by `task_id`; filter by `since`/`until` window; combined
  `status + task_id`.
- `order_by="started_at", descending=False` returns ascending; unknown
  `order_by` raises `ConfigurationError`.
- `limit=2, offset=1` returns the expected middle slice.
- `get_all_tasks(status="paused")` returns only paused.

### Pitfalls

- `since`/`until` compare against `started_at` — naive-vs-aware again:
  values loaded via the model reconstructor are aware, but the SQL
  comparison happens DB-side against stored naive text; passing an
  **aware** UTC datetime works because SQLite compares the ISO strings —
  make sure tests pass aware datetimes (API contract: aware UTC in, like
  every other datetime in the library).
- Do not add `order_by` support for arbitrary columns "while at it" —
  whitelist of two, deliberately.

---

## 3. `stats()`

### Model

In `quiv/models.py` (plain dataclass — not a SQLModel, not pydantic):

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class QuivStats:
    """Point-in-time scheduler statistics snapshot."""
    active_jobs: int                 # currently executing
    pool_size: int
    pool_utilization: float          # active_jobs / pool_size, 0.0–1.0
    tasks_by_status: dict[str, int]  # e.g. {"active": 3, "paused": 1}
    next_run_at: datetime | None     # earliest upcoming run (UTC)
    job_history_count: int           # job rows currently retained
```

Export `QuivStats` from `quiv/__init__.py`.

### Persistence helpers (reads — no write lock, per Phase 3)

```python
def count_tasks_by_status(self) -> dict[str, int]:
    # select TaskDB.status, func.count() ... group_by(TaskDB.status)

def count_jobs(self) -> int:
    # select func.count() from Job
```

(`from sqlalchemy import func` — import at top.)

### `QuivBase.stats()`

```python
def stats(self) -> QuivStats:
    with self._job_count_lock:
        active = self._active_job_count
    return QuivStats(
        active_jobs=active,
        pool_size=self._pool_size,
        pool_utilization=active / self._pool_size,
        tasks_by_status=self.persistence.count_tasks_by_status(),
        next_run_at=self.persistence.get_next_due_time(),
        job_history_count=self.persistence.count_jobs(),
    )
```

### Tests (`tests/test_base.py`)

- `test_stats_idle`: fresh scheduler → `active_jobs == 0`,
  `pool_utilization == 0.0`, empty-ish `tasks_by_status`,
  `next_run_at is None`, `job_history_count == 0`.
- `test_stats_counts_running_job`: long cooperative task running →
  `active_jobs == 1`, `tasks_by_status["running"] == 1`,
  utilization `== 1/pool_size`.
- `test_stats_next_run_at_matches_earliest_task`.

### Example app

Extend `examples/fastapi_app/routes/tasks.py` with:

- `GET /tasks/stats` → `scheduler.stats()` (dataclass → use
  `dataclasses.asdict`),
- `GET /tasks/{task_id}/jobs?limit=&offset=` → filtered
  `get_all_jobs(task_id=...)`,
- `PATCH /tasks/{task_id}` → `update_task` for `interval`/`jitter`.

This is the exit criterion from the roadmap ("admin endpoint exercising
all three").

---

## Exit checklist

- [ ] `uv run pytest` green; `uv run mypy quiv` zero errors (the
      sentinel unions are the likely trouble spot).
- [ ] `TASK_UPDATED` documented in `docs/event-listeners.md`; `update_task`,
      query params, `stats()`/`QuivStats` in `docs/api.md`; new
      "Observability" docs page (`docs/observability.md`) covering
      `stats()` + job queries + the example endpoints, added to
      `zensical.toml` nav; docs build clean.
- [ ] `CLAUDE.md`: events list gains `TASK_UPDATED`; task-identification
      bullet mentions `update_task`.
- [ ] Example app runs: `uv run uvicorn examples.fastapi_app.main:app`
      (or per the example's README) and the three new endpoints respond.
- [ ] Version `0.7.0`; roadmap page updated.
