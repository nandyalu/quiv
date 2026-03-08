# quiv

A lightweight, threadpool-backed background scheduler for Python applications.

Designed for FastAPI apps that need in-process background scheduling tied to
app lifecycle.

## Features

- Recurring and one-shot tasks (sync and async handlers)
- **Cooperative cancellation** via `_stop_event`
- **Progress callbacks** routed to your main async loop via `_progress_hook`
- Task/job persistence via SQLModel + SQLite
- Python 3.10 – 3.14

## Install

```bash
pip install quiv
```

## Quick start

```python
import asyncio
from quiv import Quiv

scheduler = Quiv(timezone_name="UTC")

def my_task(_stop_event=None, _progress_hook=None):
    for i in range(5):
        if _stop_event and _stop_event.is_set():
            return
        if _progress_hook:
            _progress_hook(step=i + 1, total=5)

async def main():
    scheduler.add_task(
        task_name="demo",
        func=my_task,
        interval=10,
    )
    scheduler.start()
    await asyncio.sleep(12)
    scheduler.shutdown()

asyncio.run(main())
```

## Documentation

Full documentation is available at
**[nandyalu.github.io/quiv](https://nandyalu.github.io/quiv/)**.

- [Getting Started](https://nandyalu.github.io/quiv/getting-started/)
- [API Reference](https://nandyalu.github.io/quiv/api/)
- [Architecture](https://nandyalu.github.io/quiv/architecture/)
- [Exceptions](https://nandyalu.github.io/quiv/exceptions/)

## License

MIT
