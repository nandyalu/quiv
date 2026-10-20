"""A handler module that builds a scheduler at import time.

This is the documented FastAPI layout: the module that holds the handlers
also runs ``scheduler = Quiv()``. A spawned child imports it again, and
the instance built there must be inert.

The test imports this module inside its body and shuts the parent's
instance down in ``finally``, because in the parent this import creates
a live scheduler with a temporary database.
"""

from __future__ import annotations

from quiv import Quiv

scheduler = Quiv()


def handler() -> None:
    """Fail unless the module-level scheduler in this process is inert."""

    if not scheduler._inert:
        raise RuntimeError("a live scheduler was built inside the worker")
