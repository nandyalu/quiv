# Phase 3 — Database Locking Rework (v0.7.0)

Single change, own phase because it is the riskiest item on the roadmap: split `PersistenceLayer`'s one global lock into lock-free reads and a serialized write path. Requires Phases 1–2 merged.

---

## Current behavior

`PersistenceLayer` (`quiv/persistence.py`) holds one `threading.Lock` (`self._lock`) around **every** method — reads and writes alike. With `pool_size` workers finalizing jobs, the scheduler thread querying due tasks, and application threads calling `get_all_jobs()`/`get_task()`, all of them serialize on one lock even though:

- the engine is created with `check_same_thread=False` and a 10 s SQLite busy timeout (`quiv/base.py`, `create_engine` call), and
- WAL mode (set via the `connect` event listener in `base.py`) explicitly allows concurrent readers alongside one writer.

## Design (prescriptive)

### 1. Classify methods

| Reads (no Python lock) | Writes (hold `_write_lock`) |
|---|---|
| `get_all_tasks` | `create_task` |
| `get_task` | `delete_task` |
| `get_job` | `queue_task_for_immediate_run` |
| `get_all_jobs` | `pause_task`, `resume_task` |
| `get_due_tasks` | `cleanup_history` |
| `get_next_due_time` | `create_job`, `mark_task_running` |
| | `finalize_task_after_job`, `mark_job_running`, `finalize_job` |

Mechanical edit: rename `self._lock` → `self._write_lock`; for every read method, change `with self._lock, Session(...)` → `with Session(...)`; write methods keep `with self._write_lock, Session(...)`.

**Read-modify-write methods stay writes.** `pause_task`, `resume_task`, `queue_task_for_immediate_run`, `finalize_task_after_job`, etc. read a row and then mutate it — they must hold the write lock for the *entire* operation (they already do; do not shrink their critical sections).

### 2. SQLite-level settings

In `QuivBase.__init__`'s connect listener (`quiv/base.py`), extend the pragma setup:

```python
@event.listens_for(self._engine, "connect")
def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()
```

- `busy_timeout=10000` matches the existing `connect_args` timeout and makes the intent explicit at the SQLite level: a writer that catches a stray `SQLITE_BUSY` retries for up to 10 s instead of raising.
- `synchronous=NORMAL` is the standard WAL pairing (fsync on checkpoint, not per-commit). The DB is an ephemeral temp file deleted on shutdown, so durability-on-crash is explicitly not a goal — this is free write throughput.

### 3. What NOT to change

- Session lifecycle: sessions stay short-lived, one per method, `expire_on_commit` default untouched. Callers receive detached objects and read plain attributes — this works today **because reads never commit**; do not add commits to read methods.
- Do not switch to a `NullPool`/`StaticPool` or change `pool_pre_ping` — out of scope; the default pool is fine with WAL.
- Do not introduce a readers-writer lock (e.g. hand-rolled RW lock). SQLite WAL *is* the reader-writer coordination; adding one in Python reintroduces the contention this phase removes.

## Stress tests (new file: `tests/test_persistence_concurrency.py`)

These are correctness tests, marked normal (they should run in CI and finish in a few seconds each).

1. `test_concurrent_writers_no_lost_updates`: one scheduler instance (not started). 8 threads × 25 iterations each calling `persistence.create_task(...)` with distinct names. Join all threads; assert exactly 200 task rows and 200 distinct ids.
2. `test_readers_during_writes_see_consistent_rows`: writer thread creating + finalizing jobs in a loop for ~2 s (create_job → mark_job_running → finalize_job); 4 reader threads hammering `get_all_jobs()` and `get_job(random_known_id)` concurrently. Assert no exceptions in any thread (collect via a shared list) and every job returned has internally consistent state (e.g. status COMPLETED ⇒ `ended_at is not None`).
3. `test_full_scheduler_under_load`: real end-to-end — scheduler with `pool_size=32`, add 32 run-once tasks with a handler that sleeps 50 ms, plus a background thread calling `get_all_jobs()` every 10 ms. Wait (deadline poll) until 32 COMPLETED jobs; assert none FAILED and no errors logged (attach a `logging.Handler` that records ERROR records to the "Quiv" logger and assert it stays empty).
4. `test_pause_resume_race_yields_valid_status`: N threads flip `pause_task`/`resume_task` on the same task id concurrently; after joining, assert status is exactly one of `paused`/`active` and `get_task` succeeds.

### Before/after measurement (goes in the PR description, not CI)

Write a throwaway script under the session scratchpad (do not commit): time 5,000 mixed operations (70 % reads / 30 % writes) across 16 threads, before and after the change. Record both numbers in the release notes for v0.7.0. Expect reads to no longer serialize; total wall time should drop substantially. If it does **not** improve, stop and investigate before merging — the likely cause is connection-pool contention, not the lock.

## Pitfalls

- **`database is locked` under pytest on slow CI**: if it appears, the busy timeout is not being applied — verify the pragma listener actually fires (it is registered per-connect; the pool may reuse connections created *before* the listener if registration order changed — keep the listener registration exactly where it is in `__init__`, before any `create_all`).
- **Do not remove the write lock "because SQLite serializes anyway"**: two concurrent Python-side read-modify-write transactions on the same row can interleave (both read, both write, last one wins). The write lock is what makes e.g. `finalize_task_after_job` vs `queue_task_for_immediate_run` safe. It stays.
- **Detached-object access**: if a test starts failing with `DetachedInstanceError`, someone added a `commit()` to a read path or changed `expire_on_commit`. Revert that; do not "fix" it by keeping sessions open longer.
- The WAL file can grow during the stress tests; irrelevant for the temp DB but if a test asserts on file sizes (none currently do), it will be flaky — don't add one.
- Threads in tests must have their exceptions captured explicitly (`try/except` appending to a shared list) — a raise inside a bare `threading.Thread` only prints to stderr and the test would silently pass.

## Exit checklist

- [ ] All four concurrency tests present and green; full suite green.
- [ ] `uv run mypy quiv` — zero errors.
- [ ] Before/after throughput numbers captured in `docs/release-notes.md` v0.7.0 section.
- [ ] `CLAUDE.md` persistence bullet updated ("Thread-safe via threading.Lock" → reads are lock-free under WAL; writes serialize on a write lock).
- [ ] Version `0.7.0`; roadmap page updated.
