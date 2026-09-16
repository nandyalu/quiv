import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


def generate_report(
    report_type: str,
    stop_event: threading.Event | None = None,
    progress_hook: Callable | None = None,
):
    """Generate a report with progress updates."""
    steps = 5
    for step in range(1, steps + 1):
        if stop_event and stop_event.is_set():
            logger.info("Report generation cancelled at step %d/%d", step, steps)
            return

        # ... do a chunk of report work ...
        time.sleep(2)  # simulate work

        if progress_hook:
            progress_hook(
                step=step,
                total=steps,
                report_type=report_type,
            )

    logger.info("Report '%s' generated successfully", report_type)
