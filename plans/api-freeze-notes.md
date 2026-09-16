# API freeze notes (Phase 6, §1)

This document records the API decisions made for `v1.0.0`. `v1.0.0` is the last release that accepts a breaking change, so each decision below is final unless a later major release revisits it.

Each section states the decision, the reason, and the migration path for users on `0.x`.

---

## 1. Injected handler parameters lose the underscore prefix

**Decision.** Rename the three parameters that quiv injects into a handler signature. Adopted 2026-09-15.

| Before (`0.x`) | After (`1.0.0`) |
| --- | --- |
| `_job_id` | `job_id` |
| `_stop_event` | `stop_event` |
| `_progress_hook` | `progress_hook` |

The old spellings are no longer injected. They are rejected instead — see [The legacy-name guard](#the-legacy-name-guard).

**Reason.** A leading underscore means "private, do not touch" in Python. These three parameters are the opposite of private. A user declares them, reads them, and builds cooperative cancellation on top of them. They are part of the public contract for writing a handler, so they now read as public names.

**Rejected alternative: accept both spellings.** Dual support would double `_INJECTABLE_KWARGS` to six members. Every handler that declares `**kwargs` receives all injectable names, so such a handler would receive six injected keys forever. The cost is permanent, and it grows the surface that `v1.0.0` is meant to freeze. A single loud break with a guard is cheaper for the user than a surface that never gets clean.

### The legacy-name guard

**Problem.** The failure mode of this rename is silent, and it breaks correctness rather than raising an error. A `0.x` handler is written like this:

```python
def handler(_stop_event=None):
    while not (_stop_event and _stop_event.is_set()):
        ...
```

After the rename quiv no longer injects `_stop_event`, so the parameter keeps its `None` default. The loop never sees a cancellation. `cancel_job()` stops working. Every `timeout` stops working, because a timeout sets the same stop event. Nothing raises and nothing is logged.

**Guard.** `add_task()` inspects the handler signature. If the handler declares `_job_id`, `_stop_event`, or `_progress_hook`, `add_task()` raises `ConfigurationError` and names the new spelling. The error is raised at registration, before the task row is written and before any job runs.

The guard only reads explicitly declared parameter names. A handler that declares `**kwargs` does not trigger it, because such a handler does not ask for a name.

**Lifetime.** The guard is a migration aid, not a deprecated API. Remove it in `2.0.0`. It is listed here so a later reader knows the removal is planned rather than forgotten.

### The collision guard

**Problem.** The underscore prefix was also reserving a namespace. Injection writes into the same dictionary that holds the `kwargs` of the user, so an injected value overwrites a user value of the same name. This was unreachable in `0.x`, because no user names a domain parameter `_job_id`. After the rename it is reachable, and `job_id` is a common domain name:

```python
scheduler.add_task("sync", handler, kwargs={"job_id": "external-123"})
```

If `handler` accepts `job_id`, quiv would replace `"external-123"` with its own job id and the user would never know.

**Guard.** `add_task()` compares the keys of `kwargs` against the injectable parameters that the handler accepts. On any overlap it raises `ConfigurationError`. A handler that declares `**kwargs` accepts all injectable names, so the guard covers that case too.

This guard is permanent. It reports a conflict that has no correct resolution: quiv cannot know which value the caller wanted.

### Migration

Rename the parameter in each handler. No other change is needed, and the behavior of each parameter is unchanged.

```python
# 0.x
def handler(_job_id=None, _stop_event=None, _progress_hook=None):
    ...

# 1.0.0
def handler(job_id=None, stop_event=None, progress_hook=None):
    ...
```

Users who miss one get a `ConfigurationError` from `add_task()` that names the parameter and its new spelling.

### Documentation scope

The rename touches the library, the tests, the examples, the documentation pages, the README, and the artifacts for AI tools. Two exceptions apply:

- **The release notes of already-released versions are immutable.** Entries for `v0.3.0` through `v0.10.0` describe the behavior of those releases and keep the old spelling. Only the new `v1.0.0` entry uses the new names.
- **`docs/roadmap.md` keeps its historical phase text.** It records what each phase shipped.

The `v1.0.0` release notes carry a breaking-change warning and a migration section. `docs/getting-started.md` and `docs/cancellation.md` carry a short migration note, because those are the pages a `0.x` user returns to.

---

## 2. `TaskNotScheduledError` was removed

**Decision.** Delete the class and its export. Adopted 2026-09-16.

`v0.9.0` deprecated the name and stopped raising it. It survived as a subclass of `TaskNotFoundError` so that `except TaskNotScheduledError` in `0.x` code kept catching. The `v0.9.0` notes announced removal in `1.0.0`, so this carries out that announcement.

Users catch `TaskNotFoundError` instead. A test asserts that the name is gone from both `quiv.exceptions` and `quiv.__all__`, so it cannot return by accident.

## 3. `run_on_main()` raises `MainLoopUnavailableError`

**Decision.** Add `MainLoopUnavailableError(QuivError, RuntimeError)` and raise it from both failure paths in `run_on_main()`. Adopted 2026-09-16.

`run_on_main()` raised a bare `RuntimeError`. It was the only exception quiv raised from outside the `QuivError` hierarchy, so `except QuivError` could not catch every quiv failure. `v1.0.0` is the last chance to close that gap.

The class inherits both parents, so the change breaks nothing:

- `except RuntimeError` — the `0.x` clause — still catches it.
- `except QuivError` now catches it as well.

Two existing tests assert `pytest.raises(RuntimeError, ...)` against these paths. Both still pass, which is the compatibility proof.

**Rejected alternative: leave it as `RuntimeError`.** The argument for leaving it is that `asyncio` raises `RuntimeError` for a missing event loop, so the bare type matches what a reader expects. The argument against won: a library with an exception hierarchy should route every failure through it, and dual inheritance keeps the familiar type available.

## 4. `__all__` audit

**Decision.** `__all__` is correct after the two changes above. Adopted 2026-09-16.

- Removed: `TaskNotScheduledError`.
- Added: `MainLoopUnavailableError`.
- Verified present: `QuivStats` and `TaskNotActiveError`, both added in earlier phases.

`QuivBase`, `PersistenceLayer`, and `ExecutionLayer` stay importable from their submodules and stay out of `__all__`. This is deliberate. They are implementation layers, and an underscore rename would break any user who already reaches for them. Leaving them unexported states the intent without breaking anyone.

## 5. Naming consistency

**Decision.** The convention holds across the public surface. One exception is recorded below. Adopted 2026-09-16.

The convention: parameters of the API are bare, and fields of a model carry their unit.

| Surface | Names | Follows the rule |
| --- | --- | --- |
| `add_task()` parameters | `interval`, `delay`, `timeout`, `retry_backoff`, `jitter` | yes |
| `add_task()` counts | `max_retries` | yes — a count has no unit |
| `Task` and `Job` fields | `interval_seconds`, `timeout_seconds`, `retry_backoff_seconds`, `jitter_seconds`, `duration_seconds` | yes |
| `QuivStats` fields | `active_jobs`, `pool_size`, `pool_utilization`, `job_history_count` | yes — none is a duration |

**The exception: `history_retention_seconds`.** This parameter of `Quiv()` and field of `QuivConfig` carries a unit, and the rule says a parameter of the API should be bare. It keeps its name. A rename would break the constructor call of every existing user for a cosmetic gain. The unit also does more work here than elsewhere: a reader can guess that `interval` is in seconds from context, but a retention window is just as plausible in days.

**Canonical names of the alias pairs.** `start()` and `shutdown()` are canonical. `startup()` is an alias of `start()`, and `stop()` is an alias of `shutdown()`. All four stay. The documentation used `start()` in 16 places and `startup()` in one; that one was changed, and `docs/getting-started.md` now states which names are canonical.

## 6. Attribute visibility

**Decision.** `registry`, `progress_callbacks`, `stop_events`, `executor`, `persistence`, and `execution` stay public and undocumented. Adopted 2026-09-16.

These are public attributes today. Renaming them with a leading underscore would break any user who already reads them, and the tests of this repository read `scheduler.registry` directly. The gain would be a signal that the documentation already sends by not mentioning them.

They are not listed in the API reference, so they carry no promise of stability. A later major release may underscore them.

## 7. Packaging

- `py.typed` ships in the wheel. The marker file exists at `quiv/py.typed`, inside the package directory, which is where `[tool.hatch.build.targets.wheel]` picks it up.
- Added the `Development Status :: 5 - Production/Stable` classifier to `pyproject.toml`. There was no status classifier before.
