from __future__ import annotations

import asyncio
import threading
from typing import cast
from typing import Any

from quiv.execution import ExecutionLayer


def test_prepare_invocation_injects_stop_and_progress_hooks() -> None:
    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def run_progress_callback(
        task_name: str, *args: Any, **kwargs: Any
    ) -> None:
        captured.append(((task_name, *args), kwargs))

    layer = ExecutionLayer(
        run_async=lambda _f, _a, _k: None,
        run_progress_callback=run_progress_callback,
    )

    def handler(_stop_event=None, _progress_hook=None):
        return None

    stop_event = threading.Event()
    args, kwargs = layer.prepare_invocation(
        task_name="demo",
        func=handler,
        args_json="[]",
        kwargs_json="{}",
        stop_event=stop_event,
    )

    assert args == []
    assert kwargs["_stop_event"] is stop_event
    kwargs["_progress_hook"](1, pct=50)
    assert captured == [(("demo", 1), {"pct": 50})]


def test_run_callable_executes_sync_function() -> None:
    layer = ExecutionLayer(
        run_async=lambda _f, _a, _k: None,
        run_progress_callback=lambda *_a, **_k: None,
    )
    result: dict[str, int] = {"value": 0}

    def handler(increment: int) -> None:
        result["value"] += increment

    layer.run_callable(handler, [3], {})
    assert result["value"] == 3


def test_run_callable_routes_async_function_to_run_async() -> None:
    called: dict[str, bool] = {"called": False}

    def run_async(_func, _args, _kwargs) -> None:
        called["called"] = True

    layer = ExecutionLayer(
        run_async=run_async, run_progress_callback=lambda *_a, **_k: None
    )

    async def handler() -> None:
        await asyncio.sleep(0)

    layer.run_callable(handler, [], {})
    assert called["called"] is True


def test_prepare_invocation_skips_optional_injections_when_not_supported() -> (
    None
):
    layer = ExecutionLayer(
        run_async=lambda _f, _a, _k: None,
        run_progress_callback=lambda *_a, **_k: None,
    )

    def handler(value: int) -> int:
        return value

    args, kwargs = layer.prepare_invocation(
        task_name="no-hooks",
        func=handler,
        args_json="[1]",
        kwargs_json="{}",
        stop_event=threading.Event(),
    )

    assert args == [1]
    assert "_stop_event" not in kwargs
    assert "_progress_hook" not in kwargs


def test_accepts_keyword_arg_handles_uninspectable_callable() -> None:
    layer = ExecutionLayer(
        run_async=lambda _f, _a, _k: None,
        run_progress_callback=lambda *_a, **_k: None,
    )
    uninspectable = cast(Any, object())
    assert layer._accepts_keyword_arg(uninspectable, "_stop_event") is False


def test_accepts_keyword_arg_returns_false_when_missing_keyword() -> None:
    layer = ExecutionLayer(
        run_async=lambda _f, _a, _k: None,
        run_progress_callback=lambda *_a, **_k: None,
    )

    def handler(value: int) -> int:
        return value

    assert layer._accepts_keyword_arg(handler, "_stop_event") is False


def test_accepts_keyword_arg_true_for_var_keyword_parameter() -> None:
    layer = ExecutionLayer(
        run_async=lambda _f, _a, _k: None,
        run_progress_callback=lambda *_a, **_k: None,
    )

    def handler(**_kwargs):
        return None

    assert layer._accepts_keyword_arg(handler, "_stop_event") is True
