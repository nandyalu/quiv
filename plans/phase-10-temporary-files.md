# Phase 10 — No leftover temporary database (v1.6.0)

This phase is what 160 files in `/tmp` taught on 2026-10-21. Each was a `quiv_<8 hex digits>.db` of 20 KB: the tables, no rows, born and last written within 200 ms. That is the footprint of a `Quiv()` that was constructed and never shut down. The source was trailarr's test suite. trailarr builds its scheduler at import time, in `tasks/__init__.py`, as the documented FastAPI layout tells it to, and only the lifespan calls `shutdown()`. A test process imports the module and never enters the lifespan, so every pytest process and every child interpreter its tests start leaves one file. Twelve files per run, two seconds apart.

quiv's own suite leaves nothing, and trailarr is fixing its tests. The pattern will come back, though, in every application that follows the documented layout, because an import-only process is normal: a test, a type checker's plugin, a script that imports the app for its models, a child interpreter. quiv owns the file, so quiv removes it. Two changes, both additive: a scheduler that was never shut down removes its database when the interpreter exits or when the object is collected, and a job that ends after `shutdown()` deleted the database no longer recreates the file. **This phase depends on nothing after Phase 7 and changes no 1.0 name or default.** It ships as `v1.6.0`, a minor release, because behavior at interpreter exit changes.

**Decisions settled on 2026-10-21** (do not reopen):

- **`weakref.finalize`, not `atexit.register` and not `__del__`.** `atexit.register` holds a strong reference, so every instance would live until exit. `__del__` does not run at exit for an object that is still referenced, and a started scheduler is referenced by its own loop thread. `weakref.finalize` holds no strong reference, runs when the object is collected, and runs at interpreter exit for every object still alive, in `atexit` order. Keep its `atexit` attribute at the default `True`.
- **The finalizer never references the scheduler strongly.** Its arguments are a `weakref.ref` to the instance, the engine, the database path, and the logger. An argument that referenced `self` would keep it alive for good.
- **A scheduler that was never started is removed quietly.** One debug line, no warning. An import-only process is a legitimate way to use quiv, and a warning at the exit of every test process is noise.
- **A scheduler that was started and never shut down is stopped at exit, with a bound of one second, and one warning.** The finalizer calls `shutdown(timeout=_EXIT_SHUTDOWN_SECONDS)` and `shutdown()` removes the file. Without the stop, the loop thread runs during the removal: it is a daemon thread, alive until the interpreter freezes it, and its next read reopens the database, because `sqlite3.connect` creates a missing file. The bound is one second, like `_PROCESS_FINALIZE_SECONDS`. The warning names the fix: call `shutdown()` in the lifespan. The jobs a one-second drain abandons were about to be frozen with the interpreter in any case.
- **`shutdown()` detaches the finalizer first and keeps its own removal and its own warning.** The existing `try` block in `shutdown()` stays, so a failed removal still warns and the existing test for it still passes. The finalizer must not run a second time after `shutdown()`, at collection or at exit.
- **A failed `__init__` removes the file too.** The engine exists before `create_all` runs, and the first connection creates the file. The `except` branch that raises `DatabaseInitializationError` calls the finalizer before it raises. The configuration checks stay where they are, before the engine, so a `ConfigurationError` creates no file; a test now asserts that.
- **A job that ends after the database is closed skips its writes.** A `_database_closed` flag, set in `shutdown()` just before the removal, makes `_execute_job` skip `finalize_job`, `finalize_task_after_job`, and the read-back. The job falls through to the in-memory `Job` path that v1.3.0 built, so the `JOB_*` event and the waiters still get a finished job. The writes went to a deleted file and recorded nothing; the only effect they had was to create the file again. A job that passes the check an instant before the flag is set still writes once. That race is accepted, and the `leftover_db_paths` fixture stays for the tests that abandon a thread.
- **No signal handling and no cleanup of files from other processes.** `SIGKILL`, `os._exit()`, and the OOM killer run no finalizer, and quiv must never delete a `quiv_*.db` it did not create: another instance in another process may own it. The docs say so.
- **Nothing changes for the worker process.** An inert instance has no engine and no file, so it registers no finalizer.

## 1. `quiv/base.py`

### Module level

Add `import weakref`. Add one constant beside `_PROCESS_FINALIZE_SECONDS`:

```python
# How long a scheduler that was started and never shut down may drain at
# interpreter exit, before its file is removed. Set by the finalizer only.
_EXIT_SHUTDOWN_SECONDS = 1.0
```

Add two module-level functions. Neither takes the scheduler.

```python
def _delete_database(engine: Engine, db_path: str) -> None:
    """Dispose the engine and remove the database file and its sidecars.

    Raises on the first failure; the caller decides how loud to be.
    """
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)
```

```python
def _finalize_scheduler(
    ref: weakref.ref[QuivBase],
    engine: Engine,
    db_path: str,
    logger: logging.Logger | logging.LoggerAdapter[Any],
) -> None:
    """Remove the database of a scheduler that shutdown() never reached.

    Runs when the instance is collected or when the interpreter exits.
    shutdown() detaches it, so it never runs after a shutdown.
    """
    scheduler = ref()
    if scheduler is not None and scheduler._started and not scheduler._shutdown:
        logger.warning(
            "shutdown() was never called. quiv stops the scheduler at"
            f" interpreter exit, waits up to {_EXIT_SHUTDOWN_SECONDS}s for"
            " its jobs, and removes its database. Call shutdown() when"
            " your application stops, in the lifespan."
        )
        scheduler.shutdown(timeout=_EXIT_SHUTDOWN_SECONDS)
        return
    try:
        _delete_database(engine, db_path)
    except Exception as e:
        logger.debug(f"Could not remove the database file {db_path}: {e}")
        return
    logger.debug(
        f"Removed the database of a scheduler that was never shut down: {db_path}"
    )
```

`ref()` is `None` when collection triggered the finalizer; at exit it returns the live instance. A live instance that was never started has no thread, so the file can go at once. `scheduler.shutdown()` removes the file itself and detaches a finalizer that is already running, which `weakref.finalize` allows: it marks the finalizer dead before it calls it.

Move the body of the removal in `shutdown()` into `_delete_database`. `shutdown()` keeps its `try`/`except` and its two log lines. The test `test_shutdown_handles_db_cleanup_failure` patches `quiv.base.os.remove`, and `_delete_database` lives in `quiv.base`, so the patch still reaches it.

### `__init__`

Right after `create_engine(...)` and before the `connect` listener, register the finalizer and the flag:

```python
self._database_closed = False
self._db_finalizer = weakref.finalize(
    self,
    _finalize_scheduler,
    weakref.ref(self),
    self._engine,
    self._db_path,
    self._logger,
)
```

In the `except Exception as e` branch that raises `DatabaseInitializationError`, call `self._db_finalizer()` before the `raise`. Calling a `finalize` object runs it once and marks it dead. At that point `_started` is `False`, so it takes the quiet branch.

The attribute name is `_db_finalizer`. Type it as `weakref.finalize` so mypy strict accepts `.detach()` and `.alive`.

### `shutdown()`

Right before the removal block:

```python
self._db_finalizer.detach()
self._database_closed = True
try:
    _delete_database(self._engine, self._db_path)
    self._logger.debug(f"Cleaned up database file: {self._db_path}")
except Exception as e:
    self._logger.warning(f"Could not cleanup database file: {e}")
```

`detach()` comes first. The flag is set before the first byte is removed, so a job that reads it after this line never writes.

Update the `timeout` paragraph of the docstring. A job that misses the deadline still runs on its daemon thread and can still log errors. Its end is no longer written to the database, so it does not create the file again; a job that began its write an instant before `shutdown()` closed the database can still do so, once.

## 2. `quiv/scheduler.py`, `_execute_job`

In the `finally` block, before `finalize_job`, read the flag once:

```python
database_closed = self._database_closed
```

When it is `True`: skip the `finalize_job` call, set `finalize_error` to a `SchedulerStoppedError("the database was closed by shutdown()")` so that the read-back is skipped as well, skip `finalize_task_after_job` (leave `will_retry` as `False`), and log one debug line: `'{task_name}' (Job {job_id}) ended after shutdown() closed the database; its end status '{status.value}' is not recorded`. The slot release, `_wake_loop()`, the registry pops, the in-memory `Job`, the events, and the waiters run as they do today. No `exception()` log, because nothing failed.

The flag is read once so that the three writes agree. Do not read `self._database_closed` three times.

The loop thread needs no check. `shutdown()` sets `_shutdown` and joins the thread before it closes the database, and the loop exits at its next check of `_shutdown`. A loop thread that outlives the timeout is already warned about and is out of scope.

## 3. `tests/conftest.py`

Update the docstring of `leftover_db_paths`. The first case shrinks: a thread abandoned by `shutdown(timeout=...)` recreates the file only when its write began before `shutdown()` closed the database, and a test cannot rule that out, so the tests that abandon a thread keep the fixture. The second case, a test that makes deletion fail, is unchanged. Add the third: a test that asserts no file is left appends the path too, so a failing assertion leaves nothing behind.

## 4. Tests

All new tests go in `tests/test_base.py` unless named otherwise. A child interpreter is `subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)`; `sys.executable` under `uv run` is the project venv, which has quiv installed. The child prints `scheduler._db_path` on its last line, and the parent asserts on that path. Poll with a deadline where a thread is involved.

1. **`test_unshutdown_scheduler_removes_its_database_when_collected`** — `Quiv()` with no `start()`, keep `_db_path`, `del` the instance, `gc.collect()`, assert the path and both sidecars are gone. The instance is in a reference cycle with its own `Thread` object, so `del` alone frees nothing; `gc.collect()` is required and the test says so in a comment.
2. **`test_unshutdown_scheduler_removes_its_database_at_exit`** — a child interpreter builds `Quiv()`, prints the path, and returns from the script. Assert exit code 0, the file gone, and nothing on stderr. This is the trailarr case.
3. **`test_started_scheduler_is_stopped_at_exit`** — a child interpreter configures `logging.basicConfig(level=logging.WARNING)`, builds `Quiv()`, adds a run-once task whose handler sleeps for 30 seconds, calls `start()`, waits until a job is running, prints the path, and returns. Assert exit code 0, the file gone, `shutdown() was never called` in stderr, and the child took less than ten seconds. The handler sleeps far longer than `_EXIT_SHUTDOWN_SECONDS`, so the test proves that the bound holds and that the abandoned job did not recreate the file.
4. **`test_database_initialization_error_leaves_no_file`** — extend `test_database_initialization_error_raised`: capture the path from the `DatabaseInitializationError` test by patching `uuid.uuid4` or by listing `quiv_*.db` in `tempfile.gettempdir()` before and after. Assert no new file.
5. **`test_configuration_error_creates_no_file`** — list `quiv_*.db` before and after `Quiv(pool_size=-1)` raises. Assert the lists are equal.
6. **`test_shutdown_detaches_the_finalizer`** — after `shutdown()`, assert `scheduler._db_finalizer.alive is False`; then `del` and `gc.collect()` and assert no error is logged (caplog at DEBUG shows no `Removed the database` line).
7. **`test_shutdown_handles_db_cleanup_failure`** — unchanged; it must still warn, because `_delete_database` raises through to `shutdown()`.
8. **`test_a_job_that_ends_after_shutdown_does_not_recreate_the_database`** — a handler waits on a `threading.Event` the test holds, not on its stop event. `start()`, wait for the job to be running, `shutdown(timeout=0.5)`, assert it returned, then set the event. Poll `caplog` for `is not recorded` with a 20-second deadline, then assert the path and sidecars do not exist. Append the path to `leftover_db_paths` so a failure leaves nothing. This test also covers the waiter: register `wait_for_job` before `shutdown()` and assert it raised `SchedulerStoppedError`, which the existing shutdown path already does.
9. **`tests/test_process_jobs.py`**: no new test. `test_shutdown_timeout_terminates_process_jobs` keeps the fixture; the note in its comment changes from "recreates the file" to "may recreate the file if its write began before the database closed".

Coverage stays at 100%: the finalizer's warning branch runs in test 3's child, and `concurrency = ["thread", "multiprocessing"]` does not cover a child started by `subprocess`. So the branch needs a parent-side test too: **`test_finalizer_stops_a_started_scheduler`** calls `_finalize_scheduler(weakref.ref(scheduler), scheduler._engine, scheduler._db_path, logger)` directly on a started scheduler and asserts the warning, `is_running is False`, and the file gone. Call it only once; `shutdown()` has detached the real finalizer by then.

## 5. Docs and artifacts

- **`docs/containers.md`**, section "The temporary database": a new paragraph after "`shutdown()` deletes all three." A scheduler that was never shut down removes its file when the interpreter exits or when the object is collected. A scheduler that was started and never shut down is stopped at exit, with a one-second bound and a warning. A process that ends by `SIGKILL`, `os._exit()`, or the OOM killer runs no cleanup, and the file stays; a `tmpfs` on `/tmp` makes that harmless.
- **`docs/api.md`** line 218 and the `shutdown()` docstring: the abandoned job no longer recreates the file, except in the race described in §1.
- **`docs/architecture.md`** lines 57 and 166: the lifecycle names the finalizer.
- **`docs/index.md`** line 208: "`shutdown()` deletes it, and so does the exit of the interpreter."
- **`docs/release-notes.md`**: a `v1.6.0` entry, two bullets, in the voice of the v1.5.0 entry. New entries only.
- **`docs/roadmap.md`**: the Phase 10 section marked complete with the date.
- **`quiv/AGENTS.md`**, **`skills/quiv/SKILL.md`**, **`docs/llms.txt`**: one line each where they describe the temporary database or `shutdown()`. `claude plugin validate .` passes.
- **`CLAUDE.md`**, the "Database lifecycle" bullet: the finalizer, `_database_closed`, and the two decisions that must not be reopened: the finalizer holds no strong reference, and a started scheduler is stopped at exit with the one-second bound.

## Pitfalls

- **An argument that references the scheduler defeats the finalizer.** `weakref.finalize(self, fn, self)` keeps `self` alive until exit. The engine does not reference the scheduler; the `connect` listener closure does not either. Pass `weakref.ref(self)`, never `self`, and never a bound method.
- **Logging at exit works, but only because of order.** `logging` registers `logging.shutdown` with `atexit` when it is imported; `weakref` registers its exit hook when the first `finalize` object is made. `atexit` runs last-registered first, so the finalizers run before the logging handlers close. quiv imports `logging` at module import, before any `Quiv()`, so the order holds. Do not log from the finalizer after the removal with anything above DEBUG for the quiet branch.
- **`sqlite3.connect` creates the file.** A read after the removal reopens the database as an empty file. That is why a started scheduler is stopped, and its loop thread joined, before the removal. Do not reorder.
- **The unstarted instance is in a reference cycle.** `self.thread = threading.Thread(target=self._loop)` holds a bound method of `self`. CPython frees the cycle at a cyclic collection, not at `del`. In tests, call `gc.collect()`. In an application, exit covers it.
- **`shutdown()` inside the finalizer must not recurse.** `weakref.finalize` removes the entry from its registry before it calls the function, so `detach()` inside `shutdown()` finds nothing and returns `None`. Do not call `self._db_finalizer()` from `shutdown()`.
- **Windows refuses to remove an open file.** `_delete_database` disposes the engine first, as the current code does. A child interpreter test must close its own handles before it exits; the default exit does.
- **Coverage does not follow `subprocess`.** The parent-side test of the warning branch in §4 is required, or the gate fails.
- **The flag is read once in `_execute_job`.** Three reads can see two values and write one of the three records, which recreates the file with half a row.
- **`_database_closed` is not `_shutdown`.** `_shutdown` is set at the start of `shutdown()`, before the drain; a job that ends during the drain must still write, because `get_job()` works until the database closes. Only the removal sets `_database_closed`.
- **Do not delete files quiv did not create.** No glob over `quiv_*.db`, in `__init__` or anywhere else. Another process owns the others.
- **One paragraph per line in every `.md` file.** zensical breaks on a hard-wrapped paragraph.

## Exit checklist

- [ ] `uv run pytest` green; coverage at or above the gate, 100% kept.
- [ ] `uv run mypy quiv` zero errors, also with `--platform win32`.
- [ ] Every test in §4 present and passing on Linux, macOS, and Windows in CI.
- [ ] `scripts/soak.py` still passes `--minutes 10`; the temp directory holds no `quiv_*.db` after it.
- [ ] A run of `uv run pytest` leaves no new `quiv_*.db` in `tempfile.gettempdir()`, checked by listing the directory before and after.
- [ ] `docs/containers.md`, `docs/api.md`, `docs/architecture.md`, `docs/index.md` updated; `uv run zensical build --clean` clean.
- [ ] `quiv/AGENTS.md`, `skills/quiv/SKILL.md`, `docs/llms.txt` updated; `claude plugin validate .` passes.
- [ ] `CLAUDE.md` updated.
- [ ] `docs/release-notes.md` entry; version `1.6.0` in `pyproject.toml` and `uv.lock`; `docs/roadmap.md` Phase 10 marked complete with the date; the row in `plans/README.md`.
