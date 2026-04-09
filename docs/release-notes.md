# Release Notes

## Unreleased

### Breaking changes

#### Task operations now use `task_id` instead of `task_name`

All public methods that previously accepted a `task_name` string now accept the
`task_id` (UUID string) returned by `add_task()`. This removes the uniqueness
constraint on task names — multiple tasks can now share the same `task_name`.

**Affected methods:**

- `remove_task(task_id)` — previously `remove_task(task_name)`
- `pause_task(task_id)` — previously `pause_task(task_name)`
- `resume_task(task_id)` — previously `resume_task(task_name)`
- `run_task_immediately(task_id)` — previously `run_task_immediately(task_name)`
- `get_task(task_id)` — previously `get_task(task_name)` (by-name lookup)

**Removed methods:**

- `get_task_by_id()` — merged into `get_task(task_id)`

**Migration:** Store the return value of `add_task()` and pass it to all
task operations:

```python
# Before
scheduler.add_task("my-task", handler, interval=60)
scheduler.pause_task("my-task")

# After
task_id = scheduler.add_task("my-task", handler, interval=60)
scheduler.pause_task(task_id)
```

#### Event listeners receive typed model objects instead of dicts

Event listener callbacks now receive `Task` and `Job` model objects directly
instead of untyped `dict[str, Any]`.

- **`TASK_*` events**: `callback(event: Event, task: Task)`
- **`JOB_*` events**: `callback(event: Event, task: Task, job: Job)`

**Migration:**

```python
# Before
def on_completed(event, data):
    print(data["task_name"], data["duration"])

# After
from quiv.models import Task, Job

def on_completed(event: Event, task: Task, job: Job):
    print(task.task_name, job.duration_seconds)
```

### What's new

#### Duplicate task names allowed

`add_task()` no longer raises `ConfigurationError` on duplicate `task_name`.
Each call returns a unique `task_id` (UUID), so multiple tasks can share a
display name. This is especially useful for one-shot tasks that may be
scheduled repeatedly with the same name.

#### `duration_seconds` and `error_message` on the Job model

The `Job` model now includes two new fields:

- `duration_seconds: float | None` — job runtime in seconds, set when the job
  finishes
- `error_message: str | None` — error description, set when a job fails

Both fields are persisted in the database and available via `get_job()` and
`get_all_jobs()`, making it easy to inspect job history without parsing logs.

#### Typed event listener callbacks

Event listeners now receive real `Task` and `Job` model objects with full IDE
autocomplete and type checking. `JOB_*` events include the parent `Task`
alongside the `Job`, so listeners have full context without extra lookups.

### Documentation

- Updated all docs to reflect `task_id`-based API across getting-started,
  API reference, architecture, event listeners, bigger applications,
  progress callbacks, and exceptions pages.
- Updated all code examples to use `task_id` for runtime operations.
- Added admonitions and footnotes throughout docs for better readability.
- Added `_job_id` tracing section to the "Why quiv?" page, describing how
  Trailarr uses injected job IDs as trace context for log correlation.
- Rewrote event listeners documentation with typed callback signatures,
  updated examples, and new FastAPI WebSocket example using model objects.

### Other changes

- Internal handler and progress callback registries are now keyed by `task_id`
  instead of `task_name`.
- `prepare_invocation()` in the execution layer uses `task_id` for progress
  callback dispatch.
- Removed `get_task_by_name()`, `get_task_id_by_name()` from the persistence
  layer.
- Renamed `get_task_by_id()` to `get_task()` in the persistence layer.
- `delete_task()` and `queue_task_for_immediate_run()` in the persistence layer
  now accept `task_id` instead of `task_name`.
- `finalize_job()` now accepts optional `duration_seconds` and `error_message`
  parameters.
- Removed unused `timezone` import from persistence module.


<!-- Placeholder for GHA to generate changelog from Github release notes -->
