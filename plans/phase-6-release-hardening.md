# Phase 6 — v1.0.0 Hardening & Release

No features. This phase turns the 0.9.0 codebase into a 1.0.0 release: API freeze, docs completion, benchmarks, coverage, and a soak test. Work through the sections in order; each has its own verifiable output.

---

## 1. API freeze review

This is the last release where breaking changes are acceptable. Produce a short review document (`plans/api-freeze-notes.md`, committed) recording every decision below, then apply them.

Checklist to review, one by one:

- [ ] **`__all__` audit**: every name in `quiv/__init__.py.__all__` is deliberate; everything public is exported (`QuivStats`, `TaskNotActiveError` from earlier phases — verify). Anything importable but internal gets a leading underscore or is left out of `__all__` knowingly.
- [ ] **Naming consistency pass** across the public surface:
      - parameter names: `interval` (add_task) vs `interval_seconds` (Task model) vs `timeout` vs `timeout_seconds` — the convention is: API params are bare (`interval`, `timeout`, `jitter`, `retry_backoff`), model fields carry units (`interval_seconds`, `timeout_seconds`, ...). Confirm every field follows it.
      - `start()`/`startup()` and `stop()`/`shutdown()` alias pairs: keep both, but pick ONE canonical name per pair in all docs and examples (`start()` / `shutdown()` — matches the FastAPI lifespan example) and say the other is an alias.
- [ ] **Attribute visibility**: `registry`, `progress_callbacks`, `stop_events`, `executor`, `persistence`, `execution` are public attributes today. Decide: they stay public-but-undocumented (cheapest, recommended — renaming breaks any existing user) — or underscore them now. Record the decision; do NOT rename without recording why.
- [ ] **Exception hierarchy**: every raise site uses the most specific exception; `docs/exceptions.md` lists all of them with when-raised.
- [ ] **Deprecations**: grep for anything marked deprecated during 0.x (`grep -ri deprecat quiv/`) and remove it.
- [ ] **`py.typed`**: verify the wheel ships type information — add a `quiv/py.typed` marker file if absent (check `[tool.hatch.build.targets.wheel]` picks it up; it does when the file lives inside the package dir).
- [ ] Classifier bump in `pyproject.toml`: `"Development Status :: 5 - Production/Stable"` (add; there is no status classifier today).

## 2. Documentation completion

- [ ] `docs/release-notes.md`: consolidated 1.0.0 entry summarizing 0.3 → 0.7 plus a short **"Migrating from 0.x"** subsection. Known behavior changes to list: `run_task_immediately` now raises `TaskNotActiveError` on non-active tasks; `shutdown(timeout=...)`; scheduler no longer polls at 1 Hz (timing-sensitive code that relied on ~1 s dispatch granularity now sees near-immediate dispatch).
- [ ] Verify pages added in earlier phases exist and are in nav: `failure-handling.md` (Phase 4), `observability.md` (Phase 5).
- [ ] README: refresh the pitch (`README.md` mirrors `docs/index.md` — keep them in sync), add a short comparison table (quiv vs `BackgroundTasks` vs APScheduler vs Celery — columns: in-process, cancellation, progress-to-loop, retries, timeout, persistence, distribution) that honestly shows what quiv does NOT do (no cron, no durable store, no multi-process).
- [ ] Every public method's docstring shows a `Raises:` section that is actually accurate (spot-check by grepping raise sites per module).
- [ ] **ASD-STE100 (Simplified Technical English) pass over user-facing docs** (`docs/*.md`; release notes and docstrings exempt): rewrite instructional prose to STE100 principles — one instruction per sentence, active voice/imperative for procedures, sentence length ≤ 20 words (procedural) / ≤ 25 (descriptive), no idioms or figurative language ("thundering herd" → "many tasks that run at the same time"), consistent approved terms (one name per concept — do not alternate e.g. "handler"/"callable"/"function" for the same thing). Established API terms and code identifiers are exempt from the approved-word list. Decided 2026-08-14; the one-paragraph-per-line rule for zensical still applies.
- [ ] `uv run zensical build --clean` — zero warnings.

## 3. Benchmarks

Create `benchmarks/` at the repo root (NOT inside `quiv/`; excluded from the wheel automatically since hatch only packages `quiv`). Two scripts, each runnable via `uv run python benchmarks/<name>.py` and printing a small table:

- [ ] `benchmarks/bench_dispatch_latency.py`: schedule 100 run-once tasks due "now" with a no-op handler; measure per-job `started_at - next_run_at` (the row is deleted for run-once, so capture the scheduled time at add time); report p50/p95/max. Target: p95 < 50 ms.
- [ ] `benchmarks/bench_throughput.py`: `pool_size=16`, 1,000 run-once no-op tasks; measure wall time from `start()` to last COMPLETED job; report jobs/second. Also run a variant with a 10 ms sleeping handler to show pool saturation behavior.
- [ ] `benchmarks/README.md`: how to run, machine caveats, and the recorded numbers for the release (update at release time).
- [ ] Copy the headline numbers into the 1.0.0 release notes.

These are scripts, not pytest — no CI integration (perf assertions in CI are flaky by nature). Do not add pytest-benchmark.

## 4. Coverage

- [ ] Add to `pyproject.toml`:

      ```toml
      [tool.coverage.report]
      fail_under = 95
      show_missing = true

      [tool.coverage.run]
      source = ["quiv"]
      ```

- [ ] `uv run pytest --cov=quiv` ≥ 95 %. Close gaps with real tests — do NOT chase 100 % by adding `# pragma: no cover` to reachable logic; pragmas are only for genuinely unreachable/defensive lines (the codebase already uses them that way).
- [ ] Wire `--cov` + fail_under into the tests CI workflow (`.github/workflows/tests.yml`) if not already there — read the workflow first; keep its existing matrix untouched.

## 5. Soak test

- [ ] `scripts/soak.py` (repo root `scripts/`, not packaged, not CI): runs a scheduler for a configurable duration (default 10 minutes, `--hours 24` for the release run) with a mixed workload — recurring sync task, recurring async task, a failing task with retries, a task cancelled periodically, a task with timeout, a sub-second task, plus a thread calling `stats()`/`get_all_jobs()` every second.
- [ ] The script self-checks and exits non-zero on: `threading.active_count()` growth beyond a fixed baseline, job_history_count exceeding what the retention window allows, any ERROR record on the "Quiv" logger (except the expected ones from the deliberately-failing task — match on message), or scheduler thread death.
- [ ] Run the 24 h soak once before tagging 1.0.0; paste the summary output into the release PR description.

## 6. Release

- [ ] Version `1.0.0` in `pyproject.toml`.
- [ ] Full gate: `uv run pytest --cov=quiv && uv run mypy quiv && uv run zensical build --clean`.
- [ ] `docs/roadmap.md`: mark all phases complete; move the page's framing to past tense ("This page tracked...") — keep the out-of-scope section as the standing record of rejected features.
- [ ] Tag + GitHub release per the repo's existing release process (check `.github/workflows/build.yml` for how publishing works before assuming anything).

## Pitfalls

- **Do not slip features in.** Anything tempting discovered during review (a cron flag, a persistence hook, an extra event) goes into a post-1.0 issue, not this phase. The rejected list in `docs/roadmap.md` is binding.
- The README and `docs/index.md` drift easily — after editing, diff the overlapping sections.
- Coverage `fail_under` will make CI red immediately if added before the gaps are closed — close gaps first, then flip the config on.
- The 24 h soak is wall-clock expensive; debug with `--minutes 10` runs, run the full one exactly once at the end.
