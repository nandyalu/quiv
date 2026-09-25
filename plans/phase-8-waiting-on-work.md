# Phase 8 — Waiting on work (v1.2.0)

Everything in this phase lets a caller wait on something and lets cancellation reach it. It comes from the 2026-09-25 review of the two applications that run quiv. In ten-acre every handler hands its work to the main loop with `run_on_main()` and returns, so quiv records a one-millisecond success while the real work runs, fails, or hangs unseen; a hung request there stalled every alarm for an evening and no quiv timeout could fire. In trailarr the HTTP endpoints that add a one-off task cannot report its result, and a cancel cannot reach the yt-dlp and ffmpeg children a job runs, so they finish or time out on their own. Neither application tests scheduling against a real instance, because nothing lets a test wait for a job.

Four additions, all additive: waiting for a job or for a task's next job (sync and async), `call_on_main()`, `run_subprocess()`, and two exceptions. No 1.0 name or default changes. **This phase does not depend on Phase 7 and ships before it**, as `v1.2.0`; the order was decided on 2026-09-25 and `plans/README.md` records it. Phase 7's §0, the three-platform CI matrix, lands before this phase because the code here is platform-sensitive.

**Decisions settled on 2026-09-25** (do not reopen):

- Waiting is by job id or by task id. `run_task_immediately()` keeps returning the row count, which the 1.0 API promised, so a caller that holds a task id waits with `wait_for_task()`.
- Four methods rather than a future-returning API: `wait_for_job()`, `await_job()`, `wait_for_task()`, `await_task()`. The sync form blocks the calling thread. The async form is a coroutine method for code on the application's loop. Both take `timeout` and raise the builtin `TimeoutError` on every supported Python: on 3.10 `concurrent.futures.TimeoutError` and `asyncio.TimeoutError` are separate classes, and quiv normalizes them.
- A waiter returns the finalized `Job`, with `status`, `duration_seconds`, and `error_message`, after quiv wrote it and after the `JOB_*` event for it was emitted. Emitted, not handled: listeners are dispatched to the main loop and may run after the waiter wakes.
- `call_on_main()` is a new function; `run_on_main()` does not change. Its signature forwards every keyword to the target, so it has no options of its own. Cancellation comes from the job's stop event, and a deadline from the task's `timeout`.
- `run_subprocess()` follows the kill rule of Phase 7 §6: a stop signal ignored for `kill_grace` seconds becomes `kill()`, after `terminate()`. The default grace is 5 s, the same number as `process_kill_grace`. A timeout keeps the stdlib contract and raises `subprocess.TimeoutExpired`, so an existing `except` clause keeps working. Only cancellation is new, and it raises `JobCancelledError`.
- Helpers find the current job's stop event through a context variable that `_run_job` sets, so code deep inside a service does not need `stop_event` in scope. The reach is the same as the active instance of `run_on_main`: nested sync calls, the event loops quiv creates on worker threads, and `asyncio.create_task` and `asyncio.to_thread`, both of which copy the context. It does not reach a `threading.Thread` the handler starts.
- `JobCancelledError` is the one exception both helpers raise when the stop event fires while they wait. The handler lets it propagate; `_run_job` already turns "stop event set" into `CANCELLED` whatever the handler raised. `SchedulerStoppedError` is what a waiter gets when `shutdown()` returns before the job finished, and what the wait methods raise after shutdown.
- Draining `run_on_main` work at shutdown stays out, as `CLAUDE.md` records. `call_on_main` exists so that work is part of the job instead.

---

## 1. Exceptions

`quiv/exceptions.py`:

```python
class JobCancelledError(QuivError):
    """Raised inside a handler by a quiv helper when the job's stop event
    was set while the helper waited.

    ``call_on_main()`` and ``run_subprocess()`` raise it. Let it
    propagate: quiv finalizes the job as ``CANCELLED``.
    """


class SchedulerStoppedError(QuivError):
    """Raised by the wait methods when ``shutdown()`` ran before the job
    finished, or when they are called after ``shutdown()``."""
```

Export both from `quiv/__init__.py` and add them to `__all__`. `docs/exceptions.md` gains both, under the existing hierarchy list.

---

## 2. Job context

`quiv/context.py` gains a second context variable and one lookup:

```python
_current_job_id: ContextVar["str | None"] = ContextVar(
    "quiv_current_job_id", default=None
)


def _current_stop_event() -> threading.Event | None:
    """The stop event of the job this code runs inside, or None."""

    # The contextvar only, never the process-level fallback: the fallback
    # is set by start() and reaches code that no job contains.
    quiv = _current_quiv.get()
    job_id = _current_job_id.get()
    if quiv is None or job_id is None:
        return None
    with quiv._registries_lock:
        return quiv.stop_events.get(job_id)
```

`Quiv._run_job` (`quiv/scheduler.py`) sets `_current_job_id` beside `_current_quiv` and resets both in the `finally`, before the `stop_events.pop`. Nothing else in `_run_job` moves.

---

## 3. Waiters

### State (`QuivBase.__init__`)

```python
# Futures resolved by _run_job when a job finalizes. Both dicts are
# guarded by _registries_lock. A key is a job id or a task id.
self._job_waiters: dict[str, list[Future[Job]]] = {}
self._task_waiters: dict[str, list[Future[Job]]] = {}
```

### Private helpers (`quiv/base.py`)

```python
def _add_waiter(self, table: dict[str, list[Future[Job]]], key: str) -> Future[Job]:
    if self._shutdown:
        raise SchedulerStoppedError("The scheduler has shut down; nothing will finish.")
    fut: Future[Job] = Future()
    with self._registries_lock:
        table.setdefault(key, []).append(fut)
    return fut

def _remove_waiter(self, table, key, fut) -> None:
    with self._registries_lock:
        waiters = table.get(key)
        if waiters is None:
            return
        if fut in waiters:
            waiters.remove(fut)
        if not waiters:
            del table[key]

def _resolve_waiters(self, table, key, job: Job) -> None:
    with self._registries_lock:
        waiters = table.pop(key, [])
    for fut in waiters:
        # A waiter that timed out, or whose coroutine was cancelled, is
        # already done; set_result on it raises InvalidStateError.
        if not fut.done():
            fut.set_result(job)

def _fail_waiters(self, table, key, exc: BaseException) -> None:
    # Same shape, set_exception instead of set_result.
```

```python
_TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

def _wait(self, fut: Future[Job], what: str, timeout: float | None) -> Job:
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # Its own class on 3.10, an alias of the builtin from 3.11.
        raise TimeoutError(f"{what} did not finish within {timeout}s") from None
```

### Public methods (`quiv/base.py`)

```python
def wait_for_job(self, job_id: str, timeout: float | None = None) -> Job:
    fut = self._add_waiter(self._job_waiters, job_id)
    try:
        job = self.get_job(job_id)  # JobNotFoundError propagates
        if job.status in _TERMINAL_JOB_STATUSES:
            return job
        return self._wait(fut, f"Job '{job_id}'", timeout)
    finally:
        self._remove_waiter(self._job_waiters, job_id, fut)
```

**Register first, then read.** That order is the whole correctness argument. `_run_job` writes the terminal status and then pops the waiters. A waiter that registered before the pop is resolved by the pop. A waiter that registered after the pop reads the terminal status and returns at once. There is no window in which a waiter can miss the job.

```python
async def await_job(self, job_id: str, timeout: float | None = None) -> Job:
    fut = self._add_waiter(self._job_waiters, job_id)
    try:
        job = self.get_job(job_id)
        if job.status in _TERMINAL_JOB_STATUSES:
            return job
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Job '{job_id}' did not finish within {timeout}s") from None
    finally:
        self._remove_waiter(self._job_waiters, job_id, fut)
```

`get_job` is a synchronous SQLite read of one row on the loop's thread. It costs what `get_job` costs from an endpoint today, well under a millisecond, and it is accepted here. `asyncio.wait_for` cancels the wrapped future on timeout and on cancellation of the awaiting coroutine, which cancels the `concurrent.futures.Future`; `_resolve_waiters` skips it because it is done. On 3.11 and later `asyncio.TimeoutError` is the builtin, so the `except` clause is harmless there and required on 3.10.

`wait_for_task(task_id, timeout=None)` and `await_task(task_id, timeout=None)` have the same shape against `_task_waiters`. The read after registration is `self.get_task(task_id)`, which raises `TaskNotFoundError` for a missing task and otherwise returns; there is no "already finished" state for a task, so the method then waits. The job a task waiter receives is **the next job of that task to finalize**: the one running at registration if there is one, otherwise the next one dispatched. Document that sentence verbatim.

### Resolution (`Quiv._run_job`, `quiv/scheduler.py`)

At the end of the `finally`, after the `JOB_*` and `JOB_RETRYING` emission:

```python
self._resolve_waiters(self._job_waiters, job_id, finalized_job)
self._resolve_waiters(self._task_waiters, task_id, finalized_job)
```

After the events, not before, so the documented order holds: a listener sees the job, then a waiter does.

### `remove_task()` (`quiv/scheduler.py`)

After the row and the registrations are gone, fail the task's waiters with `TaskNotFoundError` **unless a job of that task is running**. A running job finalizes on its own and resolves them. Check with `self.get_all_jobs(status=JobStatus.RUNNING, task_id=task_id)` before the delete, while the row still exists.

---

## 4. Shutdown

In `QuivBase.shutdown`, after both branches of the pool shutdown and before the database cleanup, pop every remaining waiter from both tables and `set_exception(SchedulerStoppedError(...))` on each that is not done. With `timeout=None` the pool drained and both tables are empty already. With a timeout, an abandoned job's waiter would otherwise block until its own timeout, or forever without one; and an abandoned job's `_run_job` can raise from `finalize_job` against the deleted database before it reaches `_resolve_waiters`, so shutdown is the only reliable place. `_shutdown = True` is already the first statement, so `_add_waiter` refuses new waiters from that point.

---

## 5. `call_on_main()`

`quiv/context.py`, exported from `quiv/__init__.py` beside `run_on_main`.

```python
_CALL_POLL_SECONDS = 0.1


def call_on_main(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` on the main loop and wait for its result.

    The sibling of :func:`run_on_main`. Same dispatch, same active
    instance, same reach; the difference is that this one waits.
    The result comes back, an exception raised by ``func`` reaches the
    caller, and the job's stop event cancels the wait.
    """
```

Resolution of the active instance and the main loop is identical to `run_on_main`, with the same two `MainLoopUnavailableError` cases. Then:

- **On the main loop's thread, sync target:** call it inline and return the result. An exception propagates to the caller. This is the same inline case `run_on_main` has, minus the logging.
- **On the main loop's thread, async target:** waiting would block the loop that must run the coroutine. Raise `MainLoopUnavailableError("call_on_main() cannot wait for an async target from the main loop's own thread; await it there instead.")`.
- **On any other thread, either target:** wrap both shapes in one coroutine so both ride `run_coroutine_threadsafe`:

```python
async def _invoke() -> Any:
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result

marker = object()
quiv._track_main_loop_work(marker)
try:
    future = asyncio.run_coroutine_threadsafe(_invoke(), main_loop)
except BaseException:
    quiv._untrack_main_loop_work(marker)
    raise
quiv._track_main_loop_work(future)
future.add_done_callback(quiv._untrack_main_loop_work)  # untrack only; the caller gets the outcome
quiv._untrack_main_loop_work(marker)

stop_event = _current_stop_event()
while True:
    try:
        return future.result(timeout=_CALL_POLL_SECONDS)
    except concurrent.futures.TimeoutError:
        if stop_event is not None and stop_event.is_set():
            future.cancel()  # cancels the task on the loop, threadsafe
            raise JobCancelledError(
                f"call_on_main({func!r}) was stopped: the job's stop event was set"
            )
```

The tracking is the same marker-then-future pattern `run_on_main` uses, so `pending_main_loop_work()` counts this work too. The done callback only untracks: an exception is the caller's, not the log's. Outside a job, `stop_event` is `None` and the loop is a plain `result()` in 100 ms slices.

An exception raised by the target propagates out of `future.result()` to the handler, so the job fails and `max_retries` applies. A task `timeout` sets the stop event, the loop above cancels the coroutine and raises, and the job finalizes as `CANCELLED` with the timeout message, exactly as a handler that honours its stop event does today.

`run_on_main`'s docstring gains one line pointing here for callers that need the result.

---

## 6. `run_subprocess()`

New module `quiv/subprocesses.py` (plural, so it never reads like the stdlib module), exported from `quiv/__init__.py`.

```python
_POLL_SECONDS = 0.1


def run_subprocess(
    args: Sequence[str] | str,
    *,
    stop_event: threading.Event | None = None,
    timeout: float | None = None,
    kill_grace: float = 5.0,
    check: bool = False,
    capture_output: bool = False,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run a child process that honours the job's stop event.

    A drop-in for ``subprocess.run`` inside a handler. When the job's
    stop event is set, the child is terminated, then killed after
    ``kill_grace`` seconds, and ``JobCancelledError`` is raised. When
    ``timeout`` passes, the child is stopped the same way and
    ``subprocess.TimeoutExpired`` is raised, as ``subprocess.run`` does.
    """
```

Behaviour, in order:

1. `stop_event` defaults to `_current_stop_event()`. Outside a job, with no event given, the helper is `subprocess.run` with the stop sequence on timeout.
2. `capture_output=True` sets `stdout` and `stderr` to `PIPE`, and passing either of those in `popen_kwargs` with `capture_output` raises `ValueError`, as the stdlib does.
3. `proc = subprocess.Popen(args, **popen_kwargs)`. A `FileNotFoundError` or any other `Popen` error propagates unchanged.
4. Loop on `proc.communicate(timeout=_POLL_SECONDS)`. The stdlib promises that after `TimeoutExpired` a retry of `communicate()` loses no output. On each `TimeoutExpired`: if `stop_event` is set, run the stop sequence and raise `JobCancelledError(f"{name} was stopped: the job's stop event was set")` where `name` is `args[0]` or `args` for a string command; else if the deadline passed, run the stop sequence and raise `subprocess.TimeoutExpired(args, timeout, output=..., stderr=...)` with whatever the final `communicate()` returned.
5. The stop sequence: `proc.terminate()`; `proc.wait(timeout=kill_grace)`; on `TimeoutExpired`, `proc.kill()` then `proc.wait()`. A grace of 0 kills on the next tick. Always `wait()` after `kill()` so the child is reaped and never a zombie.
6. `check=True` raises `subprocess.CalledProcessError` on a non-zero return code, with output attached.
7. Return `subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)`.

On Windows `terminate()` and `kill()` are the same call, so the grace is a no-op there; say so in the docstring. The helper stops the direct child only. A child that spawns its own children, as yt-dlp spawns ffmpeg, can leave them running after it dies; document `start_new_session=True` in `popen_kwargs` as the way to give the child its own process group, and note that killing the whole group is a possible later addition, not part of this phase.

---

## 7. Docs and artifacts

- `docs/api.md`: one section "Waiting for a job" with the four methods, one for `call_on_main()`, one for `run_subprocess()`, and the two exceptions in the raises lists.
- `docs/run-on-main.md`: a section "Waiting for the result: `call_on_main()`", built around the shape ten-acre has, a handler that is one hop, and what changes when the hop waits: the job carries the real duration and failure, `JOB_FAILED` fires, `max_retries` applies, and a `timeout` cancels the coroutine.
- `docs/cancellation.md`: a section "Subprocesses" with `run_subprocess()` and one "Helpers raise `JobCancelledError`" that tells a handler to let it propagate and shows what a broad `except Exception` costs.
- `docs/testing.md`: a new first section "Testing your own handlers", before "Test architecture": a real `Quiv()`, a run-once task with a short `run_at`, `start()`, `wait_for_task(task_id, timeout=5)`, and the assertions on the returned job; then the async form in a FastAPI test client. State plainly that stubbing `add_task` and friends hides scheduling bugs, which is what both applications do today.
- `docs/bigger-applications.md` §5: an endpoint that adds a one-off and awaits it with `await_task`.
- `docs/exceptions.md`, `quiv/AGENTS.md`, `skills/quiv/SKILL.md`, `docs/llms.txt`: the new names and the one-line rules.
- `CLAUDE.md` key patterns: "Waiters" (register then read; resolved after events; failed at shutdown), "Job context" (`_current_job_id`, `_current_stop_event`), and "Helpers and cancellation" (`JobCancelledError` propagates).
- `docs/release-notes.md`: a `v1.3.0` entry in Simplified Technical English.

All prose follows the global `orwell-writing` skill, one paragraph per line.

---

## 8. Tests

### `tests/test_waiting.py` (new)

All use `running_main_loop` and a 10-second deadline.

- `test_wait_for_job_returns_the_finalized_job`: a run-once task, `JOB_STARTED` listener captures the job id, `wait_for_job` returns `completed` with `duration_seconds` set.
- `test_wait_for_job_on_a_finished_job_returns_at_once`: wait after completion; returns without blocking.
- `test_wait_for_job_unknown_id_raises_job_not_found`.
- `test_wait_for_job_times_out_with_builtin_timeout_error`: a handler that sleeps 2 s, `timeout=0.2`, `pytest.raises(TimeoutError)`; then assert the waiter dict is empty (no leak).
- `test_wait_for_task_returns_the_next_job`: recurring task at 0.2 s; two consecutive waits return two different job ids in order.
- `test_wait_for_task_unknown_id_raises_task_not_found`.
- `test_wait_for_task_on_a_run_once_task_that_deletes_its_row`: the waiter registered before the run still gets the job.
- `test_remove_task_fails_idle_waiters_with_task_not_found` and `test_remove_task_leaves_waiters_of_a_running_job`.
- `test_await_job_and_await_task`: async, on `running_main_loop` through `run_coroutine_threadsafe`.
- `test_await_task_timeout_is_builtin_timeout_error`.
- `test_cancelled_await_does_not_break_resolution`: cancel the awaiting coroutine, let the job finish; no `InvalidStateError` in caplog.
- `test_listener_sees_the_job_before_the_waiter`: the listener records a timestamp; the waiter's return is later.
- `test_shutdown_with_timeout_fails_abandoned_waiters`: a handler ignoring its stop event, `shutdown(timeout=0.2)` from another thread; the waiter raises `SchedulerStoppedError`. Request `leftover_db_paths`.
- `test_wait_after_shutdown_raises_scheduler_stopped`.

### `tests/test_context.py` additions

- `test_call_on_main_returns_the_result_of_an_async_target` and `..._of_a_sync_target`.
- `test_call_on_main_propagates_the_targets_exception_and_fails_the_job`: the job is `failed` with the message.
- `test_call_on_main_is_cancelled_by_the_stop_event`: target sleeps 5 s; `cancel_job()`; the job is `cancelled` within 1 s; the task on the main loop is cancelled (a `finally` in the target sets an event).
- `test_call_on_main_is_cancelled_by_the_task_timeout`: `timeout=0.3`; the error message starts with the timeout text.
- `test_call_on_main_inline_on_the_main_loop_thread` and `test_call_on_main_async_target_on_the_main_loop_thread_raises`.
- `test_call_on_main_is_counted_by_pending_main_loop_work` and `test_call_on_main_outside_a_job_waits_without_a_stop_event`.
- `test_call_on_main_with_no_active_quiv_raises`.

### `tests/test_subprocesses.py` (new)

Every child is `sys.executable -c "<script>"`, so the tests need nothing installed.

- `test_run_subprocess_returns_completed_process_with_output`: `capture_output=True, text=True`.
- `test_run_subprocess_check_raises_called_process_error`.
- `test_run_subprocess_timeout_raises_timeout_expired_and_stops_the_child`: a sleeping child, `timeout=0.3`; `TimeoutExpired`; `proc` is gone (`returncode` set, no zombie: `os.waitpid` raises `ChildProcessError`).
- `test_run_subprocess_stop_event_terminates_and_raises_job_cancelled`: explicit `stop_event` set from a thread after 0.2 s.
- `test_run_subprocess_kills_after_the_grace_when_terminate_is_ignored`: POSIX only (`pytest.mark.skipif(sys.platform == "win32")`); the child installs `signal.signal(SIGTERM, SIG_IGN)` and sleeps; `kill_grace=0.3`; the call returns within about 1 s and the child is dead.
- `test_run_subprocess_finds_the_stop_event_from_the_job_context`: inside a real job, no `stop_event` argument; `cancel_job()` stops the child; the job is `cancelled`.
- `test_run_subprocess_outside_a_job_has_no_stop_event`.
- `test_run_subprocess_capture_output_with_stdout_kwarg_raises_value_error`.

`tests/test_base.py`: `_resolve_waiters` skips a done future; `_fail_waiters` likewise. `tests/test_exceptions.py` or the existing hierarchy test: both new exceptions subclass `QuivError`.

---

## Pitfalls

- **`set_result` on a done future raises `InvalidStateError`.** A waiter that timed out or was cancelled leaves a done future in the table until its `finally` removes it, and `_run_job` may reach it first. Guard every `set_result` and `set_exception` with `fut.done()`.
- **`TimeoutError` is three classes on 3.10.** `concurrent.futures.TimeoutError`, `asyncio.TimeoutError`, and the builtin are distinct there and aliases from 3.11. Catch the module-specific ones and raise the builtin, so the documented contract holds on every version CI runs.
- **Read after register, never before.** Swapping the two lines in a wait method opens a window where a job finalizes between the read and the registration and the waiter hangs.
- **Resolve waiters after the events.** The docs promise a listener sees the job first; moving the resolution earlier breaks a test and the promise.
- **A run-once task deletes its row at finalize.** `wait_for_task` must not re-read the task after waiting; the job it returns is proof enough.
- **`communicate()` needs `PIPE` to have anything to collect.** Without `capture_output` the child inherits the parent's descriptors and `communicate()` returns `(None, None)`; that is fine and matches `subprocess.run`. Never mix `capture_output` with explicit `stdout`/`stderr`.
- **Always `wait()` after `kill()`.** Otherwise the child stays a zombie until the interpreter exits, and a test that counts children fails later for no visible reason.
- **`future.cancel()` on a `run_coroutine_threadsafe` future is threadsafe** and cancels the task on the loop; do not reach for `loop.call_soon_threadsafe(task.cancel)`, there is no task handle to reach.
- **`call_on_main` from the loop's thread with an async target must raise, not wait.** Waiting there is a deadlock. The inline sync case stays because an endpoint may share a utility with task code, as `run_on_main` already allows.
- **The context variables do not reach a thread the handler starts.** Same limit as the active instance today. Document it once, in `run-on-main.md`, and link from `cancellation.md`.
- **The shutdown warning for main-loop work stays accurate.** `call_on_main` work is tracked, and because the job waits on it, a job that is abandoned by `shutdown(timeout=...)` is exactly a case where the count is non-zero afterwards. That is correct behaviour, not a bug.

---

## Exit checklist

- [ ] `uv run pytest` green; coverage at or above the gate.
- [ ] `uv run mypy quiv` zero errors.
- [ ] Every test in §8 present and passing; the three timeout tests pass on 3.10 as well as 3.14.
- [ ] `docs/testing.md` opens with "Testing your own handlers", and the example in it runs as written.
- [ ] `docs/api.md`, `docs/run-on-main.md`, `docs/cancellation.md`, `docs/exceptions.md`, `docs/bigger-applications.md` updated; `uv run zensical build --clean` clean.
- [ ] `quiv/AGENTS.md`, `skills/quiv/SKILL.md`, `docs/llms.txt` updated; `claude plugin validate .` passes.
- [ ] `CLAUDE.md` key patterns updated.
- [ ] `docs/release-notes.md` entry; version bumped; `docs/roadmap.md` Phase 8 marked complete with the date.
- [ ] `trailarr/quiv_findings.md` and `ten-acre/quiv_findings.md` items for this phase are now actionable; tick nothing there, that is the applications' work.
