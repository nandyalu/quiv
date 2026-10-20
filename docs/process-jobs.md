# Process Jobs

quiv keeps its thread pool and, from v1.5.0, has a second pool: processes. A task chooses one pool with `executor="thread"` or `executor="process"`. A process job runs in a process that quiv spawns for that job alone, and quiv can terminate that process. That is the one thing a thread can never offer. Everything else stays in the parent: the job state, the temporary database, the events, the retries, and the progress callback.

```python
from quiv import Quiv

scheduler = Quiv(pool_size=10, process_pool_size=2)


def transcode(path: str, stop_event=None, progress_hook=None) -> None:
    ...  # CPU work, or a library that cannot be stopped from the outside


scheduler.add_task("transcode", transcode, run_once=True, args=(path,), executor="process")
```

## When to use a process job

A process job is the right choice in two cases:

- **The work needs the CPU.** The standard CPython build runs one thread of Python code at a time. A thread job that computes for a minute holds the whole application back for that minute. A process job computes on another core.
- **The work cannot be stopped from the inside.** A C extension that runs for an hour, a library call with no timeout, or code you do not own and cannot change. A thread job like that ignores its stop event and keeps its slot. A process job gets `process_kill_grace` seconds to stop, and then quiv terminates the process.

A thread job is the right choice for everything else. It starts in about a millisecond; a process job starts in about 200 ms on a desktop, because quiv spawns a new interpreter and imports your module into it. A thread job shares the memory of the application, so it can read a cache, hold a connection, or update a dict. A process job sees none of that.

| | Thread job | Process job |
| --- | --- | --- |
| Start cost | about 1 ms | about 200 ms, see [The cost](#the-cost-of-a-process-job) |
| Runs in parallel with Python code in the application | no | yes |
| quiv can end it | no, it asks and waits | yes, after the grace |
| Shares memory with the application | yes | no |
| Handler can be | any callable | a function a new process can import by name |
| Arguments | pickled and unpickled in the same process | pickled here, unpickled in the new process |

## Configuration

Two values join the configuration. Both are keyword-only on `Quiv()`, and both are fields on `QuivConfig`.

- `process_pool_size`, default 0: the largest number of process jobs at once. 0 means no process pool, which is the state of every application that upgrades.
- `process_kill_grace`, default 5.0: the seconds a process job may ignore its stop event before quiv terminates the process. 0 terminates on the next tick after the signal.

```python
scheduler = Quiv(pool_size=10, process_pool_size=2, process_kill_grace=5.0)
# or
scheduler = Quiv(config=QuivConfig(pool_size=10, process_pool_size=2))
```

`pool_size` may be 0 when `process_pool_size` is above 0. The two cannot both be 0. An unused pool creates no thread.

## Choosing the pool for a task

`add_task()` and `update_task()` take `executor`, either `"thread"` or `"process"`, or the `Executor` enum. The default follows the pools: thread when `pool_size` is above 0, else process. There is no setting that changes the default.

```python
from quiv import Executor

scheduler.add_task("report", build_report, interval=3600, executor=Executor.PROCESS)
scheduler.update_task(task_id, executor="thread")  # from the next dispatch on
```

An `executor` that names a pool of size 0 raises `ConfigurationError` at the call. A change through `update_task()` takes effect at the next dispatch; a running job finishes where it started. `Task.executor` and `Job.executor` show which pool a task uses and which pool a job ran on.

Backpressure is per pool. A full thread pool does not hold back a due process task, and a full process pool does not hold back a due thread task. A process job that starts late because the process pool was full logs a warning that names `process_pool_size`.

## What a handler may be

A new process must import the handler by name. `add_task()` checks that at the call, and `update_task(executor="process")` checks it too, so a handler that cannot cross is refused before a job exists.

Accepted:

- A function at module level.
- A `staticmethod`, passed through its class: `Handlers.run`.

Rejected, with `ConfigurationError`:

- A lambda.
- An inner function.
- A bound method and a classmethod.
- A `functools.partial` object.
- An instance with `__call__`.

A handler defined in a script's `__main__` passes the check. In the new process it works only when the script guards its entry point with `if __name__ == "__main__":`. Without the guard, the new process runs the script again. The documented FastAPI layout, where uvicorn imports the application module, needs no guard.

The handler's module is imported again in the new process, with everything that module imports. Keep the import of a handler module cheap, and keep side effects out of it; the [inert scheduler](#the-scheduler-in-the-new-process) covers the one side effect the FastAPI layout always has.

## What crosses the boundary

| Item | How it crosses |
| --- | --- |
| `args` and `kwargs` | Pickled in the parent, unpickled in the new process. Every class they use must be importable there under the same module path. |
| `job_id` | Injected as in a thread job. |
| `stop_event` | A `multiprocessing` event with the same `is_set()` and `wait()` as a `threading.Event`. A handler written for threads runs unchanged. |
| `progress_hook` | The payload is pickled and sent to the parent, which runs the progress callback as it does for a thread job: on the main loop, with the same fallbacks. A payload that cannot be pickled raises `QuivError` in the handler, and the job fails. |
| `run_on_main()` | The function and its arguments are pickled and sent to the parent, which runs the function on the main loop. The function must be importable by name, like a handler; a lambda raises `QuivError` in the handler. A missing main loop cannot raise in the handler, because the message has already been sent: the parent logs the error instead. |
| `run_subprocess()` | Works. The job's stop event reaches it through the process, so a cancel stops the child of the child too. |
| `call_on_main()` | Does not work from a process job. There is no main loop in the new process, and the call raises `MainLoopUnavailableError`. |
| An async handler | Runs in a fresh event loop in the new process, as a thread job's handler runs in a fresh loop on its thread. |
| A return value | Ignored, as in a thread job. |
| An exception | Its message becomes `error_message`, the same text a thread job records. Its traceback goes to the parent's log. |

Inside the new process, `run_on_main()` and `run_subprocess()` reach every thread of that process, including a `threading.Thread` the handler starts. That is wider than their reach in a thread job.

Two things a process job cannot do. It cannot start `multiprocessing` children of its own, because quiv starts it as a daemon process; use `subprocess` or `run_subprocess()` for a child program. And it cannot see the application's memory: a cache, a connection, or a global that the application filled after import is not there.

## The kill rule

For a process job, a stop signal that the handler ignores for `process_kill_grace` seconds becomes `terminate()`. The signal comes from any of these, and the rule is the same for each:

| Trigger | Signal | Then |
| --- | --- | --- |
| `timeout` | the loop sets the stop event | terminate after the grace |
| `cancel_job()` | the caller sets the stop event | terminate after the grace |
| `remove_task()` | quiv sets the stop event of the running job | terminate after the grace |
| `shutdown(timeout=None)` | quiv sets every running job's stop event | terminate after the grace |
| `shutdown(timeout=T)` | same | terminate after the grace, or at `T`, whichever is first |

A handler that honors its stop event exits before the grace ends, and quiv terminates nothing. A grace of 0 terminates on the next tick after the signal. A terminated job finalizes as `cancelled`; when a timeout caused it, the error message starts with the timeout text, as it does today. Thread jobs are unchanged: quiv never kills a thread.

A process that dies on its own, by a segfault, `os._exit()`, or the OOM killer, is a `failed` job with the exit code or signal in `error_message`, so `max_retries` applies. The cancelled-overrides-failed rule still holds: a child that dies after its stop event was set finalizes as `cancelled`.

## Outcomes

| What happened | Status | `error_message` |
| --- | --- | --- |
| The handler returned | `completed` | `None` |
| The handler raised | `failed` | the exception's message |
| The process died on its own | `failed` | `process ended with exit code 3 before reporting a result`, or `process killed by signal 9` |
| The handler saw its stop event and returned | `cancelled` | `None`, or the timeout text |
| The handler ignored its stop event and quiv terminated the process | `cancelled` | `process terminated: ignored its stop event for 5.0s`, after the timeout text when a timeout set the event |
| `shutdown(timeout=T)` terminated the process at `T` | `cancelled` | `process terminated: shutdown timeout` |

`Job.pid` holds the process id. It is `None` in the `JOB_STARTED` event, because the process starts after that event, and set in every later read.

## The scheduler in the new process

Spawn imports the handler's module in the new process. In the documented FastAPI layout that module runs `scheduler = Quiv()` at import time. Inside a process that quiv spawned, that instance is **inert**: it has no database, no pools, and no loop thread, and it does not register itself as the active instance. `add_listener()` and `remove_listener()` still work, so a module that registers listeners at import time still imports.

Every other method raises `WorkerProcessError`, which names the fix: create and start the scheduler in the parent process only, for example in the FastAPI lifespan. A module that calls `scheduler.start()` at import time therefore fails to import in the new process, and the job finalizes as `failed` with that message.

## Logging inside the new process

quiv does not forward the log records of the new process to the parent. The new interpreter starts with Python's default logging configuration, so a handler's log lines go to `stderr`, or nowhere, until the handler's module configures logging. Configure logging in the module that holds the handler, or at the top of the handler, if those lines matter to you. quiv's own lines about the job, its start, its end, and its termination, come from the parent and go through the parent's logger.

## The cost of a process job

quiv takes the start time of a job before it spawns the process, so `duration_seconds` of a process job includes the spawn: a new interpreter, the import of quiv, and the import of the handler's module. `benchmarks/bench_process_overhead.py` measures it. On an Intel Core i5-11600 with Python 3.10 on Linux, a no-op process job takes about 220 ms at the median and a no-op thread job about 1 ms. Windows and macOS spawn more slowly.

quiv uses the `spawn` start method on every platform. Forking a parent that already runs threads and holds SQLite connections can deadlock, so `fork` is out. `forkserver` would cut the start cost on Linux and macOS; it is a possible later option, not implemented.

## Platforms

The test suite runs on Linux, macOS, and Windows, on Python 3.10 through 3.14, and the process-job tests run on all three. Two differences show in the records. After `terminate()`, Windows reports a large positive exit code where Linux and macOS report a signal; quiv reads neither to decide that it terminated a process, so `error_message` is the same on each. And a process killed by a signal reports `process killed by signal 9` on Linux and macOS and an exit code on Windows.
