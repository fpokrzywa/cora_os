"""Current-date awareness for the LLM.

The local model only knows its training cutoff, so every prompt sent to it gets
a current date/time preamble built here. Computed at call time (never cached) so
it stays correct across a long-running process. Timezone is settings.cora_timezone
(default UTC); falls back to UTC if the name can't be resolved.
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

logger = logging.getLogger(__name__)


def current_tz():
    name = settings.cora_timezone or "UTC"
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "CORA_TIMEZONE=%r could not be resolved; falling back to UTC "
            "(is the tzdata package installed?)",
            name,
        )
        return timezone.utc


def current_datetime_preamble() -> str:
    """A single authoritative line stating the current date/time, prepended to
    every system prompt sent to the model."""
    now = datetime.now(current_tz())
    stamp = now.strftime("%A, %B %d, %Y at %-I:%M %p %Z")
    return (
        f"Current date and time: {stamp}. This is the present moment — treat it "
        "as authoritative for anything time-relative (\"today\", \"now\", "
        "\"latest\", \"recent\", ages, durations, deadlines). Do not use your "
        "training-data cutoff as the current date."
    )
