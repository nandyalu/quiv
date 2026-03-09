# Contributing to quiv

Thanks for your interest in contributing! This guide covers the process and standards for the project.

## Getting started

1. Fork the repository and clone your fork
2. Install dependencies for development:

   ```bash
   uv pip install -e ".[dev]"
   ```

3. Create a branch for your changes:

   ```bash
   git checkout -b your-branch-name
   ```

## Development workflow

### Running tests

All tests must pass before submitting a PR:

```bash
uv run pytest
```

Run with coverage to check for gaps:

```bash
uv run pytest --cov=quiv
```

### Type checking

```bash
uv run mypy quiv
```

### Building docs

```bash
uv run zensical build --clean
```

## Coding standards

- Follow existing code style and patterns in the codebase
- Use type annotations for all function signatures
- Use `str, Enum` for enum classes (not `StrEnum`) to maintain Python 3.10 compatibility
- Use SQLModel's `col()` wrapper for typed WHERE clauses in persistence code
- Keep handler injection parameters prefixed with `_` (`_stop_event`, `_progress_hook`)
- All internal datetime handling must use UTC — the `timezone` parameter is for log display only
- Do not set log levels in library code — that is the application's responsibility

## Pull request process

1. Ensure all tests pass and type checking is clean
2. Update documentation if your change affects public API, behavior, or configuration
3. Add tests for new functionality or bug fixes
4. Keep commits focused — one logical change per commit
5. Write clear commit messages describing **why**, not just what
6. Open a PR against the `main` branch with a description of the change

## What to include in your PR description

- Summary of the change and motivation
- How to test it
- Any breaking changes or migration notes

## Reporting bugs and requesting features

Use the [issue templates](https://github.com/nandyalu/quiv/issues/new/choose) on GitHub. Check existing issues before opening a new one.

## Code of conduct

This project has a [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to follow it.
