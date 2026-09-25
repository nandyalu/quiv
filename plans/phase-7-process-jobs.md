# Phase 7 — Process jobs (v1.4.0)

quiv keeps its thread pool and gains a second pool: processes. A task chooses one pool with `executor="thread"` or `executor="process"`. A process job runs in a process that quiv spawns for that job alone, and quiv can terminate it. That is the one thing a thread can never offer. Everything else — job state, the temporary database, events, retries, and the progress callback — stays in the parent. Requires Phase 6 merged (`v1.0.0`).

This is the first phase after the API freeze. Every change is additive. No 1.0 name changes, no removed parameter, no changed default for an existing parameter.

**Decisions settled on 2026-09-17** (do not reopen):

- One process per job, spawned by quiv and bounded by `process_pool_size`. Never a reused worker pool: a reused pool cannot kill one job, and one crashed worker breaks the pool.
- Start method `spawn` on every platform, forced through `multiprocessing.get_context("spawn")`. Forking a parent that already runs threads and holds SQLite connections can deadlock. `forkserver` is documented as a possible later option, not implemented.
- Two sizes and one per-task switch. `process_pool_size` joins the config, default 0. `add_task` and `update_task` gain `executor`. The default per task is thread unless `pool_size` is 0, then process. No `default_executor` parameter.
- One kill rule and one config value. `process_kill_grace` in seconds, default 5. A stop signal that a process job ignores for that long becomes `terminate()`. This covers timeout, `cancel_job()`, `remove_task()`, and shutdown with the same rule.
- A `Quiv()` built inside a worker process is inert. Spawn re-imports the module that holds the handler, and in the documented FastAPI pattern that module also runs `scheduler = Quiv()`.
- `job_id`, `stop_event`, `progress_hook`, and `run_on_main()` all work from a process job. Child logging is documented, not forwarded.
- CI runs on Linux, macOS, and Windows.
- Durable persistence and lazy or unpicklable arguments stay out of scope. The reasons are recorded in `docs/roadmap.md`.

---

## 0. CI on three platforms (do this first, as its own commit)

The existing suite has only ever run on Ubuntu. Windows file locking and macOS runner speed can break tests that have nothing to do with this phase. Make the current suite green on all three platforms before writing a line of process code, so a later failure is attributable.

`.github/workflows/tests.yml`:

```yaml
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]
```

The coverage-comment step gains `matrix.os == 'ubuntu-latest'` in its `if:`. `typecheck.yml` stays on Ubuntu but runs mypy twice: once as today and once with `--platform win32`, so a `multiprocessing` type that differs on Windows fails before CI does.

Known likely fixes on Windows: `shutdown()` deletes the database file after `engine.dispose()`; any test that holds a `Session` open across `shutdown()` gets `PermissionError`. Timing tests that assume a sub-second dispatch on a loaded runner need the generous poll-with-deadline pattern from `plans/README.md` rule 5.

---

## 1. Config and constructor

### `quiv/config.py`

```python
process_pool_size: int = 0
process_kill_grace: float = 5.0
```

Docstring lines: `process_pool_size` — maximum process jobs at once; 0 means no process pool. `process_kill_grace` — seconds a process job may ignore its stop event before quiv terminates the process.

### `QuivBase.__init__` and `Quiv.__init__`

Both gain the same keyword-only parameters after `*`, before `logger`:

```python
process_pool_size: int = 0,
process_kill_grace: float = 5.0,
```

The config-exclusivity check adds `or process_pool_size != 0 or process_kill_grace != 5.0` to its condition and names the new parameters in its message.

Validation, replacing the existing `pool_size <= 0` check:

```python
if pool_size < 0:
    raise ConfigurationError("pool_size must be greater than or equal to 0")
if process_pool_size < 0:
    raise ConfigurationError(
        "process_pool_size must be greater than or equal to 0"
    )
if pool_size == 0 and process_pool_size == 0:
    raise ConfigurationError(
        "pool_size and process_pool_size cannot both be 0"
    )
if process_kill_grace < 0:
    raise ConfigurationError(
        "process_kill_grace must be greater than or equal to 0"
    )
```

`tests/test_base.py::test_quiv_base_validates_pool_size_and_history` still passes: `Quiv(pool_size=0)` alone leaves both pools at 0.

New runtime attributes, set next to `self.executor`:

```python
self._process_pool_size = process_pool_size
self._process_kill_grace = process_kill_grace
self._default_executor = (
    Executor.THREAD if pool_size > 0 else Executor.PROCESS
)
# ThreadPoolExecutor rejects max_workers=0. A pool of size 0 accepts no
# task (add_task refuses the executor), so an unused pool creates no thread.
self.executor = ThreadPoolExecutor(max_workers=max(pool_size, 1))
self._process_waiters = ThreadPoolExecutor(
    max_workers=max(process_pool_size, 1),
    thread_name_prefix="quiv-process-waiter",
)
self._mp_context = multiprocessing.get_context("spawn")
self._job_processes: dict[str, BaseProcess] = {}  # job_id -> child
self._terminated_jobs: dict[str, str] = {}  # job_id -> reason
self._active_process_count = 0
```

`_job_processes` and `_terminated_jobs` are guarded by `_registries_lock`; `_active_process_count` by `_job_count_lock`. `BaseProcess` comes from `multiprocessing.process`. The `stop_events` annotation widens to `dict[str, threading.Event | ProcessEvent]`, where `ProcessEvent` is imported only under `TYPE_CHECKING` from `multiprocessing.synchronize` — see Pitfalls for why that import must stay lazy.

A waiter thread is the parent-side half of every process job: it spawns the child, relays its messages, runs the kill timer, and finalizes the job. Waiters live in their own pool so a process job never consumes a thread-pool slot.

---

## 2. Models and persistence

### `quiv/models.py`

```python
class Executor(str, Enum):
    """Where a task's jobs run.

    Attributes:
        THREAD (str): On the thread pool, inside the scheduler's process.
        PROCESS (str): In a new process that quiv spawns for each job.
    """

    THREAD = "thread"
    PROCESS = "process"
```

`TaskDB` gains `executor: str = Executor.THREAD` (mirror the `status: str = TaskStatus.ACTIVE` style). `Task` gains the same field, and `_convert_from_task_db` adds `"executor": data.executor` to its hand-built dict — the Phase 4 pitfall applies; the round-trip test proves it.

`Job` gains `executor: str = Executor.THREAD` and `pid: int | None = None`. The pid is set right after the child starts, so `JOB_STARTED` carries `pid=None` and every later read carries the number.

`QuivStats` gains three fields with defaults, at the end, so the dataclass stays constructible the old way:

```python
process_pool_size: int = 0
active_process_jobs: int = 0
process_pool_utilization: float = 0.0
```

`quiv/__init__.py` exports `Executor` and `WorkerProcessError` (§8).

### `quiv/persistence.py`

- `create_task(...)` gains `executor: str = Executor.THREAD` and passes it to `TaskDB(...)`.
- `create_job(task_id, task_name, attempt=1, executor: str = Executor.THREAD)`.
- New `set_job_pid(self, job_id: str, pid: int) -> None`: write lock, `JobNotFoundError` when the row is missing, sets `job.pid`.
- `get_next_due_time(self, executor: str | None = None)`: when given, adds `.where(TaskDB.executor == executor)`. A bare value comparison, per the predicate conventions in `CLAUDE.md`.
- `update_task(**column_updates)` needs no change; `executor` is a plain column.

---

## 3. Validation in `add_task` and `update_task`

Module-level helpers in `quiv/scheduler.py`, next to the existing validators:

```python
def _resolve_executor(value: Executor | str) -> Executor:
    try:
        return Executor(value)
    except ValueError:
        raise ConfigurationError(
            "executor must be 'thread' or 'process'"
        ) from None


def _validate_pool_available(
    executor: Executor, pool_size: int, process_pool_size: int
) -> None:
    if executor is Executor.THREAD and pool_size == 0:
        raise ConfigurationError("executor='thread' requires pool_size > 0")
    if executor is Executor.PROCESS and process_pool_size == 0:
        raise ConfigurationError(
            "executor='process' requires process_pool_size > 0"
        )


_PROCESS_HANDLER_HELP = (
    "executor='process' requires a handler that a new process can import"
    " by name: a module-level function or a staticmethod. A lambda, an"
    " inner function, a bound method, a functools.partial object, or a"
    " callable instance cannot be sent to a process."
)


def _resolve_handler_reference(func: Callable[..., Any]) -> tuple[str, str]:
    """Return ``(module, qualname)`` for a handler a child can import."""

    module_name = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None)
    if (
        not isinstance(module_name, str)
        or not isinstance(qualname, str)
        or "<" in qualname  # "<lambda>" and "<locals>"
    ):
        raise ConfigurationError(_PROCESS_HANDLER_HELP)
    try:
        target: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as e:
        raise ConfigurationError(
            f"{_PROCESS_HANDLER_HELP} Cannot import"
            f" '{module_name}:{qualname}': {e}"
        ) from e
    if target is not func:
        raise ConfigurationError(_PROCESS_HANDLER_HELP)
    return module_name, qualname
```

The `is not func` check is what rejects bound methods and classmethods: the walk yields the plain function or a fresh bound-method object, never the object the caller passed. A staticmethod reached through its class yields the function itself and passes. A handler defined in `__main__` passes in the parent; the child side works only when the script guards its entry point — see Pitfalls.

### `add_task`

New keyword-only parameter after `jitter`: `executor: Executor | str | None = None`. Docstring: which pool runs this task's jobs; `None` picks thread when the thread pool has a size above 0, else process. Insert after the `progress_callback` callable check, before args are pickled:

```python
resolved_executor = (
    self._default_executor
    if executor is None
    else _resolve_executor(executor)
)
_validate_pool_available(
    resolved_executor, self._pool_size, self._process_pool_size
)
if resolved_executor is Executor.PROCESS:
    _resolve_handler_reference(func)
```

Pass `executor=resolved_executor.value` to `create_task`. The "Task added" log line gains `" in a new process"` for process tasks. The `Raises:` section names the importability failure.

### `update_task`

New keyword-only parameter `executor: Executor | str | _Unset = _UNSET`. When passed: resolve, validate the pool, and when the result is `PROCESS`, run `_resolve_handler_reference` on the registered handler (same lookup the `kwargs` branch already does; a missing handler is left to the persistence call). Write `updates["executor"] = resolved.value`. The change takes effect at the next dispatch; a running job finishes where it started.

---

## 4. The child side: `quiv/_worker.py` (new module)

Everything that runs in the spawned process lives here, and nothing else imports the scheduler. The parent passes only strings, bytes, the stop event, and one pipe end, so the child unpickles nothing until the worker flag is set. That ordering is the whole point of the module: the handler's module — and every module that its arguments need — is imported after `_in_worker` is `True`, so a module-level `Quiv()` in the app becomes inert (§8).

```python
"""Child side of a process job. Runs in the spawned interpreter only."""

from __future__ import annotations

import importlib
import traceback
from multiprocessing.connection import Connection
from typing import Any, Callable, cast

from .exceptions import QuivError
from .execution import ExecutionLayer, run_in_fresh_loop

_in_worker = False
_channel: Connection | None = None


def in_worker_process() -> bool:
    """Return ``True`` inside a quiv worker process."""

    return _in_worker


def channel() -> Connection | None:
    """Return the pipe to the parent, or ``None`` outside a worker."""

    return _channel


def send(message: tuple[Any, ...], what: str) -> None:
    """Send one message to the parent.

    Raises:
        QuivError: If ``message`` cannot be pickled, or no channel exists.
    """

    if _channel is None:
        raise QuivError("no parent channel: not inside a quiv worker process")
    try:
        _channel.send(message)
    except Exception as e:
        raise QuivError(
            f"{what} must be picklable in a process job: {e}"
        ) from e


def _send_progress(task_id: str, *args: Any, **kwargs: Any) -> None:
    send(("progress", args, kwargs), "progress_hook payload")


def import_handler(module_name: str, qualname: str) -> Callable[..., Any]:
    target: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return cast("Callable[..., Any]", target)


def run_job(
    conn: Connection,
    stop_event: Any,
    job_id: str,
    task_id: str,
    module_name: str,
    qualname: str,
    args_pickled: bytes,
    kwargs_pickled: bytes,
) -> None:
    """Process entry point: run one handler and report the outcome."""

    global _in_worker, _channel
    _in_worker = True  # before the handler's module is imported
    _channel = conn
    try:
        func = import_handler(module_name, qualname)
        layer = ExecutionLayer(run_in_fresh_loop, _send_progress)
        f_args, f_kwargs = layer.prepare_invocation(
            task_id=task_id,
            func=func,
            args_pickled=args_pickled,
            kwargs_pickled=kwargs_pickled,
            stop_event=stop_event,
            job_id=job_id,
        )
        layer.run_callable(func, f_args, f_kwargs)
    except Exception as e:
        conn.send(("error", str(e), traceback.format_exc()))
    else:
        conn.send(("done",))
    finally:
        conn.close()
```

Messages, child to parent, always a tuple whose first item is the kind:

| Message | Meaning |
| --- | --- |
| `("progress", args, kwargs)` | The handler called `progress_hook(*args, **kwargs)`. |
| `("run_on_main", func, args, kwargs)` | The handler called `run_on_main(func, *args, **kwargs)`. |
| `("done",)` | The handler returned. |
| `("error", message, traceback_text)` | The handler raised; `message` is `str(exc)`. |

The child reuses `ExecutionLayer` unchanged, so injection, the legacy-name rules, and the `tuple(args)` contract are identical in both pools. The stop event the child receives is a `multiprocessing` event with the same `is_set()` / `wait()` methods, so a handler written for threads runs unchanged.

### `quiv/execution.py`

Extract the body of `QuivBase.run_async` into a module-level function so the parent and the child share one implementation, and the per-invocation fresh loop (the isolation requirement) holds in both:

```python
def run_in_fresh_loop(
    task: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> None:
    async def invoke() -> Any:
        return await task(*(args or ()), **(kwargs or {}))

    asyncio.run(invoke())
```

`QuivBase.run_async` becomes a one-line delegation. Its docstring and signature stay.

---

## 5. Dispatch and the parent-side waiter

### `_loop` (`quiv/scheduler.py`)

Backpressure is per pool. A full thread pool must not block a due process task, so the loop skips instead of breaking:

```python
def _pool_has_slot(self, executor: str) -> bool:
    with self._job_count_lock:
        if executor == Executor.PROCESS:
            return self._active_process_count < self._process_pool_size
        return self._active_job_count < self._pool_size
```

```python
now = self._now_utc()
if self._pool_has_slot(Executor.THREAD) or self._pool_has_slot(
    Executor.PROCESS
):
    for task in self.persistence.get_due_tasks(now):
        if not self._pool_has_slot(task.executor):
            continue
        self._dispatch_due_task(task, now)
```

The `# pragma: no cover` on the old `break` goes away; the backpressure test covers the `continue`.

### `_compute_sleep_seconds`

The saturation rule from Phase 2 becomes per pool. Otherwise a saturated thread pool with an overdue thread task would busy-poll at 100 Hz even though the process pool is free, which is exactly the case the existing comment warns about:

```python
open_pools = [
    executor
    for executor in (Executor.THREAD, Executor.PROCESS)
    if self._pool_has_slot(executor)
]
if len(open_pools) == 2:
    next_due = self.persistence.get_next_due_time()
elif len(open_pools) == 1:
    next_due = self.persistence.get_next_due_time(executor=open_pools[0])
else:
    next_due = None
```

`stats()` keeps the unfiltered call.

### `_dispatch_due_task`

After `create_job(..., executor=task.executor)`:

```python
is_process = task.executor == Executor.PROCESS
stop_event: threading.Event | ProcessEvent = (
    self._mp_context.Event() if is_process else threading.Event()
)
with self._registries_lock:
    self.stop_events[job_id] = stop_event
    if task.timeout_seconds is not None:
        self._job_deadlines[job_id] = time.monotonic() + task.timeout_seconds

if is_process:
    self._logger.info(
        f"Scheduling task '{task.task_name}' (Job ID: {job_id}) to run"
        " now in a new process"
    )
    with self._job_count_lock:
        self._active_process_count += 1
    self._process_waiters.submit(
        self._run_process_job,
        job_id, task.id, task.task_name, task.run_once, now,
        task_snapshot, func, task.args, task.kwargs, stop_event,
    )
    return
# the existing thread path: prepare_invocation, executor.submit(_run_job, ...)
```

`prepare_invocation` is not called in the parent for a process job. The child does it, after the worker flag is set, so the argument classes are imported there.

### `_run_job` becomes `_execute_job` plus two thin wrappers

Move the body of `_run_job` into `_execute_job(self, job_id, task_id, task_name, run_once, scheduled_at, task_snapshot, executor: Executor, run: Callable[[], str | None])`. `run` does the work and returns a note for `error_message`, or `None`. Then:

```python
def _run_job(self, job_id, task_id, task_name, run_once, scheduled_at,
             task_snapshot, func, args, kwargs) -> None:
    def run() -> str | None:
        self.execution.run_callable(func, args, kwargs)
        return None

    self._execute_job(job_id, task_id, task_name, run_once, scheduled_at,
                      task_snapshot, Executor.THREAD, run)


def _run_process_job(self, job_id, task_id, task_name, run_once,
                     scheduled_at, task_snapshot, func, args_pickled,
                     kwargs_pickled, stop_event) -> None:
    def run() -> str | None:
        return self._run_in_child(
            job_id, task_id, func, args_pickled, kwargs_pickled, stop_event
        )

    self._execute_job(job_id, task_id, task_name, run_once, scheduled_at,
                      task_snapshot, Executor.PROCESS, run)
```

Changes inside `_execute_job`, relative to today's `_run_job`:

1. The late-start warning names the pool: `"process pool was busy. Consider increasing process_pool_size."` for a process job.
2. `note = run()` inside the `try`.
3. A new branch before `except Exception`, for a handler that raised in the child or a child that died:

   ```python
   except _ChildFailure as e:
       end_time = self._now_utc()
       duration = end_time - start_time
       job_error = e
       detail = f"\n{e.traceback_text}" if e.traceback_text else ""
       self._logger.error(
           f"'{task_name}' (Job {job_id}) failed in its process at"
           f" {self._to_display_timezone(end_time)}"
           f" [runtime: {duration}]: {e}{detail}"
       )
       status = JobStatus.FAILED
   ```

   `logger.exception` is wrong here: the parent-side traceback is the waiter's, not the handler's. The child's traceback text is what the log needs.
4. In `finally`, the counter decrement picks the pool:

   ```python
   with self._job_count_lock:
       if executor is Executor.PROCESS:
           self._active_process_count -= 1
       else:
           self._active_job_count -= 1
   ```
5. The error message assembly takes the note into account. `error_message = str(job_error) if job_error is not None else note`. When `timed_out and status == CANCELLED`: `detail = f"handler raised: {error_message}" if job_error is not None else note`, then `error_message = timeout_message if detail is None else f"{timeout_message} ({detail})"`. A timed-out and terminated process job therefore reads `Job exceeded timeout of 0.3s (process terminated: ignored its stop event for 0.5s)`.

`_ChildFailure` lives in `quiv/scheduler.py`, private:

```python
class _ChildFailure(Exception):
    """A process job's handler raised, or its process died."""

    def __init__(self, message: str, traceback_text: str | None = None):
        super().__init__(message)
        self.traceback_text = traceback_text
```

`str(e)` is the child's `str(exc)`, so `error_message` reads the same for both pools: the exception's message, not its type. The type name is in the traceback text in the log.

### `_run_in_child` — spawn, relay, kill timer

```python
_PROCESS_POLL_SECONDS = 0.05
```

```python
def _run_in_child(
    self, job_id, task_id, func, args_pickled, kwargs_pickled, stop_event
) -> str | None:
    """Run the handler in a spawned process and relay its messages.

    Returns a note for ``error_message`` when quiv terminated the
    process, else ``None``. Raises ``_ChildFailure`` when the handler
    raised or the process died on its own.
    """

    module_name, qualname = _resolve_handler_reference(func)
    parent_conn, child_conn = self._mp_context.Pipe(duplex=False)
    process = self._mp_context.Process(
        target=_worker.run_job,
        args=(child_conn, stop_event, job_id, task_id, module_name,
              qualname, args_pickled, kwargs_pickled),
        name=f"quiv-job-{job_id}",
        daemon=True,
    )
    process.start()
    # Drop the parent's copy of the child's end, or EOF never arrives
    # when the child exits.
    child_conn.close()
    with self._registries_lock:
        self._job_processes[job_id] = process
    if process.pid is not None:
        self.persistence.set_job_pid(job_id, process.pid)

    outcome: tuple[Any, ...] | None = None
    kill_at: float | None = None
    try:
        while True:
            # The kill timer runs every iteration, so a child that sends
            # progress without pause cannot starve it.
            if outcome is None and stop_event.is_set():
                if kill_at is None:
                    kill_at = time.monotonic() + self._process_kill_grace
                elif time.monotonic() >= kill_at:
                    self._terminate_job_process(
                        job_id, process,
                        f"ignored its stop event for {self._process_kill_grace}s",
                    )
                    kill_at = None
            ready = wait(
                [parent_conn, process.sentinel], timeout=_PROCESS_POLL_SECONDS
            )
            if parent_conn in ready:
                try:
                    message = parent_conn.recv()
                except EOFError:
                    break
                outcome = self._handle_child_message(task_id, message) or outcome
                if outcome is not None:
                    kill_at = None  # the handler finished; the child is exiting
                continue
            if process.sentinel in ready:
                # The process ended. Take what it sent before it went.
                while parent_conn.poll():
                    try:
                        message = parent_conn.recv()
                    except EOFError:
                        break
                    outcome = self._handle_child_message(task_id, message) or outcome
                break
    finally:
        process.join()
        parent_conn.close()
        with self._registries_lock:
            self._job_processes.pop(job_id, None)
            reason = self._terminated_jobs.pop(job_id, None)

    if outcome is not None and outcome[0] == "error":
        raise _ChildFailure(outcome[1], outcome[2])
    if outcome is not None:
        return None
    if reason is not None:
        return f"process terminated: {reason}"
    code = process.exitcode
    if code is None or code >= 0:
        raise _ChildFailure(
            f"process exited with code {code} before reporting a result"
        )
    raise _ChildFailure(f"process killed by signal {-code}")
```

`wait` is `multiprocessing.connection.wait`; it takes a pipe end and a process sentinel together on every platform. The kill check comes before the wait on purpose: `kill_at` is set on one iteration and fires on a later one, so a grace of 0 terminates on the next tick.

```python
def _handle_child_message(
    self, task_id: str, message: tuple[Any, ...]
) -> tuple[Any, ...] | None:
    """Relay one child message. Returns the message when it is final."""

    kind = message[0]
    if kind == "progress":
        self.run_progress_callback(task_id, *message[1], **message[2])
        return None
    if kind == "run_on_main":
        try:
            run_on_main(message[1], *message[2], **message[3])
        except MainLoopUnavailableError as e:
            self._logger.error(
                f"run_on_main() from a process job of task '{task_id}'"
                f" failed: {e}"
            )
        return None
    return message
```

The waiter runs inside `_execute_job`, where `_current_quiv` is already set, so `run_on_main` from the parent side finds this instance the ordinary way. A relayed `progress` goes through `run_progress_callback` exactly as a thread job's does: same main-loop dispatch, same fallbacks.

`_terminate_job_process` lives in `QuivBase`, because `shutdown()` needs it too:

```python
def _terminate_job_process(
    self, job_id: str, process: BaseProcess, reason: str
) -> None:
    with self._registries_lock:
        self._terminated_jobs[job_id] = reason
        stop_event = self.stop_events.get(job_id)
    if stop_event is not None:
        stop_event.set()  # a terminated job always finalizes as cancelled
    if process.is_alive():
        process.terminate()
    self._logger.warning(
        f"Job {job_id}: {reason}; process {process.pid} terminated."
    )
```

---

## 6. The kill rule (document verbatim)

For a process job, a stop signal that the handler ignores for `process_kill_grace` seconds becomes `terminate()`. The signal comes from any of these, and the rule is the same for each:

| Trigger | Signal | Then |
| --- | --- | --- |
| `timeout` | the loop sets the stop event | terminate after the grace |
| `cancel_job()` | the caller sets the stop event | terminate after the grace |
| `remove_task()` | quiv sets the stop event of the running job | terminate after the grace |
| `shutdown(timeout=None)` | quiv sets every running job's stop event | terminate after the grace |
| `shutdown(timeout=T)` | same | terminate after the grace, or at `T`, whichever is first |

A handler that honors its stop event exits before the grace ends, and quiv terminates nothing. A grace of 0 terminates on the next tick after the signal. A terminated job finalizes as `CANCELLED`; when a timeout caused it, the error message starts with the timeout text, as it does today. Thread jobs are unchanged: quiv never kills a thread.

A process that dies on its own — a segfault, `os._exit()`, the OOM killer — is a `FAILED` job with the exit code or signal in `error_message`, so `max_retries` applies. The cancelled-overrides-failed rule from Phase 4 still holds: a child that dies after its stop event was set finalizes as `CANCELLED`.

---

## 7. Shutdown

`QuivBase.shutdown` already sets the stop event of every running job. Extend the drain:

```python
if timeout is None:
    self.executor.shutdown(wait=True)
    # Every waiter terminates its child process_kill_grace seconds after
    # the stop signal above, so this wait is bounded for process jobs.
    self._process_waiters.shutdown(wait=True)
else:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with self._job_count_lock:
            remaining = self._active_job_count + self._active_process_count
        if remaining <= 0:
            break
        time.sleep(0.05)
    with self._registries_lock:
        processes = dict(self._job_processes)
    for job_id, process in processes.items():
        self._terminate_job_process(job_id, process, "shutdown timeout")
    if processes:
        # Termination is immediate. Give the waiters a moment to record
        # the outcome before the database goes away.
        finalize_deadline = time.monotonic() + _PROCESS_FINALIZE_SECONDS
        while time.monotonic() < finalize_deadline:
            with self._job_count_lock:
                if self._active_process_count <= 0:
                    break
            time.sleep(0.05)
    # existing warning for undrained thread jobs, plus one for process
    # jobs still finalizing; then shutdown(wait=False) on both pools.
```

`_PROCESS_FINALIZE_SECONDS = 1.0` in `quiv/base.py`. A waiter's remaining work after termination is a join, two database writes, and event emission, so one second is generous. A listener that blocks is the same hazard it is for thread jobs today.

---

## 8. The inert scheduler in a worker process

Spawn imports the handler's module in the child. In the documented FastAPI layout that module runs `scheduler = Quiv()` at import time, and uvicorn imports the app module rather than running it, so a `__main__` guard does not help. Without this section every process job would build a second scheduler with its own temporary database that nothing ever deletes.

First statements of `QuivBase.__init__`, before any validation:

```python
self._inert = _worker.in_worker_process()
if self._inert:
    self._logger = logger or logging.getLogger("Quiv")
    self._event_listeners = {}
    self._event_listeners_lock = threading.Lock()
    return
```

An inert instance has no engine, no pools, no loop thread, and does not register itself as the active instance. `add_listener` and `remove_listener` keep working, so an app that registers listeners at import time still imports. Everything that needs the engine or a pool calls this first:

```python
def _ensure_live(self) -> None:
    if self._inert:
        raise WorkerProcessError(
            "This Quiv instance was created inside a quiv worker process,"
            " where it is inert: the process runs one job and has no"
            " scheduler. Create and start the scheduler in the parent"
            " process only, for example in the FastAPI lifespan."
        )
```

Call sites, one line at the top of each: `start`, `shutdown`, `add_task`, `update_task`, `remove_task`, `pause_task`, `resume_task`, `run_task_immediately`, `cancel_job`, `get_task`, `get_job`, `get_all_tasks`, `get_all_jobs`, `stats`. The aliases `startup` and `stop` go through `start` and `shutdown`.

`quiv/exceptions.py`:

```python
class WorkerProcessError(QuivError):
    """Raised when scheduler methods are called inside a quiv worker process.

    A process job runs in a process that quiv spawned for that job. A
    ``Quiv`` created there is inert; the scheduler lives in the parent.
    """
```

A module that calls `scheduler.start()` at import time fails to import in the child. The job then finalizes as `FAILED` with this error's message, which names the fix.

---

## 9. `run_on_main()` from a process job

`quiv/context.py`, first statements of `run_on_main`:

```python
if _worker.channel() is not None:
    _worker.send(
        ("run_on_main", func, args, kwargs),
        "run_on_main() function and arguments",
    )
    return
```

The function crosses the pipe by reference, so it must be importable by name like a process handler; a lambda raises `QuivError` in the handler. The parent runs it on the main loop through the ordinary `run_on_main`. One difference from a thread job, to document: a missing main loop cannot raise in the child, because the message has already been sent. The parent logs the error instead.

Inside the child the channel is a module global, so `run_on_main` reaches every thread of that process, including a `threading.Thread` the handler starts itself. That is wider than the thread-pool rule in the `run_on_main` docstring, and the docs say so.

---

## 10. `stats()`

```python
with self._job_count_lock:
    active = self._active_job_count
    active_processes = self._active_process_count
return QuivStats(
    active_jobs=active,
    pool_size=self._pool_size,
    pool_utilization=active / self._pool_size if self._pool_size else 0.0,
    tasks_by_status=self.persistence.count_tasks_by_status(),
    next_run_at=self.persistence.get_next_due_time(),
    job_history_count=self.persistence.count_jobs(),
    process_pool_size=self._process_pool_size,
    active_process_jobs=active_processes,
    process_pool_utilization=(
        active_processes / self._process_pool_size
        if self._process_pool_size
        else 0.0
    ),
)
```

The division guard is required now that `pool_size` may be 0.

---

## 11. Coverage across the process boundary

`pyproject.toml`:

```toml
[tool.coverage.run]
source = ["quiv"]
concurrency = ["thread", "multiprocessing"]
sigterm = true
```

`concurrency = multiprocessing` makes coverage add itself to the spawn preparation data, so the child measures `quiv/_worker.py`; it implies `parallel = true`, and pytest-cov combines the data files. `sigterm = true` makes a child that quiv terminates write its data first on POSIX. On Windows `terminate()` gives the child no chance, so the lines that a killed child was executing are measured by the tests where the child finishes on its own. The gate stays at 95 in `pyproject.toml`; do not lower it, and do not add pragmas to reachable child code.

---

## 12. Tests

### Handler modules

A spawned child imports handlers by module name, so every handler in these tests is a module-level function in a helper module, not a closure in a test. `tests/` has no `__init__.py`; pytest's default import mode puts `tests/` on `sys.path`, the child inherits `sys.path`, and the handler's `__module__` is the bare module name in both. Name the modules so they cannot collide with anything else on the path:

- `tests/quiv_process_handlers.py`: `noop()`, `write_pid(path)`, `fail_until(counter_path, succeed_on)`, `crash(code)` calling `os._exit(code)`, `wait_for_stop(stop_event)`, `sleep_ignoring_stop(seconds)`, `report_progress(progress_hook, count)`, `progress_unpicklable(progress_hook)`, `call_run_on_main(marker_path)` with a module-level target `write_marker(path)`, `run_on_main_lambda()`, `async_noop()`, `class Holder` with a `staticmethod`, a plain method, and a `classmethod`.
- `tests/quiv_process_handlers_with_scheduler.py`: `scheduler = Quiv()` at module level and `def handler()` that raises unless `scheduler._inert` is `True`. The test imports it inside the test body and shuts the parent's instance down in `finally`, because in the parent that import creates a live scheduler with a temp database.

### `tests/test_process_jobs.py` (new)

All use the `running_main_loop` fixture and poll with a 20-second deadline; spawn on a Windows runner can take more than a second per job.

- `test_process_job_completes_and_records_pid`: run-once process task; job `completed`, `executor == "process"`, `pid` is an int other than `os.getpid()`, and the pid the handler wrote to a file matches.
- `test_process_job_exception_is_failed_with_message_and_traceback`: `error_message == "boom"`; caplog holds the handler's function name from the child's traceback.
- `test_process_job_retries_then_succeeds`: `fail_until` with a counter file; `max_retries=1`, `retry_backoff=0.1`; one `failed` job, then one `completed`, attempts 1 and 2.
- `test_process_job_crash_is_failed`: `crash(3)`; `failed`; message contains `"exit code 3"`.
- `test_process_job_cooperative_cancel_is_not_terminated`: `wait_for_stop`; `cancel_job()`; `cancelled`, `error_message is None`, exit code 0.
- `test_process_job_timeout_terminates_after_grace`: `sleep_ignoring_stop(30)`, `timeout=0.3`, `process_kill_grace=0.5`; terminal within ~3 s, `cancelled`, message contains both `"timeout"` and `"terminated"`, `multiprocessing.active_children()` empty.
- `test_process_kill_grace_zero_terminates_on_next_tick`.
- `test_remove_task_terminates_running_process_job`.
- `test_progress_hook_crosses_the_pipe`: async progress callback on the main loop records three payloads in order.
- `test_unpicklable_progress_payload_fails_the_job`: message names `progress_hook payload`.
- `test_run_on_main_from_process_job`: marker file written in the parent's process (assert the pid inside it is the parent's).
- `test_run_on_main_lambda_in_process_job_raises`.
- `test_async_handler_in_process_job`.
- `test_backpressure_is_per_pool`: `pool_size=1, process_pool_size=1`; a 3-second thread job must not delay a due process task, and a 3-second process job must not delay a due thread task (compare `started_at` and `ended_at`).
- `test_late_start_warning_names_process_pool` (caplog).
- `test_shutdown_timeout_terminates_process_jobs`: `process_kill_grace=30`, `sleep_ignoring_stop(30)`, `shutdown(timeout=0.5)` returns within ~2 s and `active_children()` is empty. Request `leftover_db_paths` — see the Testing section of `CLAUDE.md`.
- `test_shutdown_without_timeout_waits_for_grace_kill`: `process_kill_grace=0.3`; `shutdown()` returns within ~2 s.
- `test_stats_reports_process_pool`.
- `test_module_level_quiv_is_inert_in_child`: the second handler module; the job completes.
- `test_inert_instance_raises_on_lifecycle_calls`: `monkeypatch.setattr(_worker, "_in_worker", True)`; `Quiv()` has `_inert`; `start()`, `add_task()`, `stats()` raise `WorkerProcessError`; `add_listener` works. Always restore through `monkeypatch`, never by assignment — a leaked flag makes every later `Quiv()` in the session inert.
- `test_worker_run_job_reports_done_and_error`: call `_worker.run_job` in-process with a stub connection that records `send` calls and a `threading.Event`; assert `("done",)` for `noop` and an `("error", ...)` tuple for a bad qualname. Restore `_worker._in_worker` and `_worker._channel` through `monkeypatch`.

Validation tests, in `tests/test_scheduler.py`: lambda, inner function, bound method, classmethod, `functools.partial`, and callable instance are rejected for `executor="process"` while a staticmethod is accepted; `executor="process"` with `process_pool_size=0` and `executor="thread"` with `pool_size=0` raise; `executor="bogus"` raises; the default executor is `process` when `pool_size=0`; `update_task(executor="process")` runs the handler check and `get_task().executor` reflects the change; `Quiv(config=QuivConfig(process_pool_size=1), process_pool_size=1)` raises the exclusivity error.

`tests/test_base.py`: `pool_size=0, process_pool_size=0` raises; `process_kill_grace=-1` raises. `tests/test_models.py` and `tests/test_persistence.py`: `executor` round-trips through `create_task`, `get_task`, and `get_all_tasks`; `set_job_pid`; `get_next_due_time(executor=...)` filters. `tests/test_config.py`: the new defaults.

---

## 13. Soak and benchmark

`scripts/soak.py` gains `--process-pool-size` (default 2) and two tasks: `process_task` doing a CPU-bound loop for about 100 ms every 3 s, and `process_timeout_task` sleeping 30 s and ignoring its stop event, with `timeout=2.0` and interval 8 s, so the kill path runs hundreds of times in a long soak. The admin poller samples `len(multiprocessing.active_children())` and the script fails when the peak exceeds `process_pool_size`, or when any child is alive after `shutdown`. The thread bound becomes `max(baseline_threads, 4 + pool_size + process_pool_size) + 5`, because the waiter pool adds up to `process_pool_size` threads. The script already guards its entry point, which spawn requires.

`benchmarks/bench_process_overhead.py`: 50 run-once no-op process jobs, one at a time; report p50 and p95 of `duration_seconds`, which for a process job includes the spawn. Publish the numbers in the release notes so the cost is stated, not implied.

---

## 14. Docs and artifacts

- New page `docs/process-jobs.md`, in the `zensical.toml` nav after "Cancellation". Sections: when a process job is the right choice and when it is not; `process_pool_size`, `process_kill_grace`, and a thread pool of size 0; `executor` per task and the default rule; what a handler may be, with the rejected forms and the `__main__` guard; what crosses the boundary (arguments, `stop_event`, `progress_hook` payloads, `job_id`, `run_on_main` and its importable function, return values ignored, exceptions returned as text); the kill rule table from §6; the outcomes table (completed, failed by exception, failed by crash, cancelled by stop event, cancelled by termination); the inert scheduler; logging inside a child; the spawn cost and `forkserver` as a later option; the platforms CI tests.
- `docs/api.md`: constructor and `QuivConfig` parameters, `add_task(executor=)`, `update_task(executor=)`, `Executor`, `Task.executor`, `Job.executor` and `Job.pid`, the three `QuivStats` fields, `WorkerProcessError`.
- `docs/failure-handling.md`: the timeout section gains the process case. `docs/cancellation.md`: `cancel_job()` on a process job. `docs/run-on-main.md`: from a process job. `docs/exceptions.md`: `WorkerProcessError`. `docs/architecture.md`: `_worker.py`, the pipe, and the waiter. `docs/observability.md`: the new stats fields. `docs/testing.md`: the three-platform matrix.
- README and `docs/index.md` (keep them in sync): the pitch sentence "It is a scheduler for one process, backed by a thread pool" gains the optional process pool; the comparison row "Spreads work over processes or machines" becomes "processes on this machine, yes; other machines, no"; the "One process" caveat becomes a "One machine" caveat that names process jobs.
- `quiv/AGENTS.md`, `skills/quiv/SKILL.md`, `docs/llms.txt`: `executor="process"`, the importable-handler rule, and the kill rule.
- `docs/release-notes.md`: a `v1.4.0` entry, in Simplified Technical English, with the benchmark number.
- `CLAUDE.md`: architecture gains `_worker.py`; key patterns gain a "Process jobs" bullet; the testing section gains the coverage concurrency setting and the CI matrix.

All prose follows the global `orwell-writing` skill, one paragraph per line.

---

## Pitfalls

- **Order in the child is everything.** `_in_worker = True` must run before the handler's module is imported, and the parent must pass the handler as strings and the arguments as bytes for that to be possible. Pass the function object as a `Process` argument and unpickling imports the app module before `run_job` runs, and the inert scheduler never happens.
- **Do not import `multiprocessing.synchronize` at module level.** On a platform without a working `sem_open` (some serverless runtimes) that import raises `ImportError`, and quiv would fail to import for a user who never asked for a process. Keep it under `TYPE_CHECKING`; `get_context("spawn")` at init is safe, and `ctx.Event()` is only called on a process dispatch.
- **Close the parent's copy of the child's pipe end** right after `start()`. Otherwise the pipe never reaches EOF when the child dies and the waiter waits on the sentinel alone, which works but drops any message still buffered.
- **The `__main__` handler.** A handler defined in a script's `__main__` passes validation in the parent. In the child, spawn runs the script as `__mp_main__` and aliases it, so it works only when the script guards its entry point with `if __name__ == "__main__":`. Without the guard the child re-runs the script. Document this; do not try to detect it.
- **Windows exit codes.** After `terminate()` a Windows child reports a large positive exit code, not a negative signal number. `_run_in_child` never derives "terminated" from the exit code; it reads `_terminated_jobs`.
- **Spawn cost lands in `duration_seconds`.** `start_time` is taken before the spawn, so a process job's duration includes it. The benchmark publishes the number; the docs say it.
- **Coverage of the child.** Without `concurrency = ["thread", "multiprocessing"]` the child's lines count as unexecuted and the gate fails at once. Set the config before writing the tests.
- **pytest import mode.** The handler-module trick relies on pytest's default `prepend` import mode. Do not switch the repository to `--import-mode=importlib`; the child could not import the handler modules by the name the parent used.
- **A leaked worker flag** turns every later `Quiv()` in the test session into an inert instance. Tests that touch `_worker._in_worker` or `_worker._channel` restore both through `monkeypatch`.
- **`daemon=True` on the child** only guarantees termination when the parent interpreter exits normally. A parent killed with SIGKILL leaves the child to finish or hang; `shutdown()` is the correct exit path, as it is for the database file.
- **Do not add a manager process.** The stop event and the pipe are inherited at `Process` creation, which needs no manager. A `multiprocessing.Manager` would be a third long-lived process to leak.
- **Pickled argument classes.** Arguments are unpickled in the child, so any class they use must be importable there by the same module path. That is already true for the pickle in `add_task`; the difference is that the import now happens in a fresh interpreter, so a class defined in a test function fails. Test handlers take primitives and paths.
- **`update_task(executor="process")` on a task with a lambda handler** must fail at the call, not at dispatch. The handler check in `update_task` is not optional.

---

## Exit checklist

- [ ] §0 landed first as its own commit: `tests.yml` runs the existing suite green on `ubuntu-latest`, `macos-latest`, and `windows-latest` across Python 3.10–3.14; `typecheck.yml` runs mypy for `win32` as well.
- [ ] `uv run pytest` green on all three platforms, including every test in §12; coverage at or above the gate with the multiprocessing concurrency setting in place.
- [ ] `uv run mypy quiv` — zero errors, on both platforms.
- [ ] `Task` round-trip test proves `executor` survives `get_task()` and `get_all_tasks()`; `Job` carries `executor` and `pid`.
- [ ] A 10-minute soak with `--process-pool-size 2` passes: no child alive after shutdown, peak children within the bound, no thread growth.
- [ ] `benchmarks/bench_process_overhead.py` committed; its numbers are in the release notes.
- [ ] `docs/process-jobs.md` in the nav; every page in §14 updated; `uv run zensical build --clean` clean; README and `docs/index.md` diffed against each other.
- [ ] `quiv/AGENTS.md`, `skills/quiv/SKILL.md`, `docs/llms.txt` updated; `claude plugin validate .` passes.
- [ ] `CLAUDE.md` updated per §14.
- [ ] Version `1.4.0`; `docs/roadmap.md` Phase 7 marked complete with the date.
