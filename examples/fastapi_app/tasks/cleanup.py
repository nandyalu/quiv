import logging
import threading
import time

logger = logging.getLogger(__name__)


def cleanup_stale_records(
    days: int,
    _stop_event: threading.Event | None = None,
):
    """Delete records older than `days` from the database."""
    batches = 10
    for batch in range(1, batches + 1):
        if _stop_event and _stop_event.is_set():
            logger.info("Cleanup cancelled at batch %d/%d", batch, batches)
            return

        # ... delete a batch of old records ...
        time.sleep(1)  # simulate work

    logger.info("Cleanup finished: processed %d batches", batches)
