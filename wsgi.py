"""WSGI entrypoint for production servers (e.g. gunicorn on Koyeb).

Procfile/start command:
    gunicorn -w 1 --threads 4 --timeout 60 -b 0.0.0.0:${PORT:-5057} wsgi:app

Set POLLER_AUTOSTART=1 in the deployment environment to run the snapshot
poller in a background daemon thread inside the same process. Keep workers=1
so we don't run N parallel pollers and trip API rate limits.

NOTE: do NOT put startup logic inside `if __name__ == "__main__"` — gunicorn
imports this module without executing the __main__ guard.
"""
import logging
import os
import threading

from dashboard.app import app  # noqa: F401  re-exported for gunicorn

log = logging.getLogger("wsgi")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _poll_forever():
    from ingest.poller import run_forever
    try:
        run_forever()
    except Exception:
        log.exception("background poller thread crashed")


_started = False
if os.environ.get("POLLER_AUTOSTART") == "1" and not _started:
    t = threading.Thread(target=_poll_forever, name="da-poller", daemon=True)
    t.start()
    _started = True
    log.info("Background poller thread started inside gunicorn worker.")
