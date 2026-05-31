"""Container liveness check for cora-worker.

The worker has no HTTP server, so the cora-api image's baked-in healthcheck
(curl :8000/health) can never pass for it. The worker refreshes a heartbeat
file every poll iteration; this check fails if that file is missing or stale,
which means the poll loop has stalled or died.
"""

import os
import sys
import time

HEARTBEAT_FILE = os.environ.get(
    "WORKER_HEARTBEAT_FILE", "/tmp/cora-worker.heartbeat"
)
POLL_INTERVAL = float(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "5"))
# Tolerate a few missed beats before declaring the worker unhealthy.
MAX_AGE_SECONDS = max(30.0, POLL_INTERVAL * 4 + 10.0)


def main() -> int:
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except OSError:
        print(f"heartbeat file missing: {HEARTBEAT_FILE}", file=sys.stderr)
        return 1
    if age > MAX_AGE_SECONDS:
        print(
            f"heartbeat stale: age={age:.1f}s > {MAX_AGE_SECONDS:.1f}s",
            file=sys.stderr,
        )
        return 1
    print(f"ok: heartbeat age={age:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
