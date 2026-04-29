"""Top-level shim — re-exports for `pip install git+...da-exchange-dashboard`.

Typical use from a parent Flask app (e.g. claude-projects):

    from da_exchange_dashboard import create_blueprint, start_poller_thread
    app.register_blueprint(create_blueprint("da"), url_prefix="/da")
    start_poller_thread()  # spawns the snapshot loop in-process
"""
import logging
import os
import threading

from .app import create_app, create_blueprint

__all__ = ["create_app", "create_blueprint", "start_poller_thread"]

_log = logging.getLogger("da_exchange_dashboard")
_poller_started = False
_poller_lock = threading.Lock()


def start_poller_thread() -> bool:
    """Idempotently spawn the snapshot poller in a background daemon thread.

    Returns True if the thread was started by this call, False if a poller
    was already running in this process. Safe to call multiple times — only
    the first call starts the thread.

    Set DA_DB_PATH to a writable location on the host (e.g. /tmp/da.db on
    Koyeb's ephemeral filesystem) so the poller can persist snapshots.
    """
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return False

        from .ingest.poller import run_forever

        def _loop():
            try:
                run_forever()
            except Exception:
                _log.exception("DA poller thread crashed")

        t = threading.Thread(target=_loop, name="da-poller", daemon=True)
        t.start()
        _poller_started = True
        _log.info("DA snapshot poller thread started (db=%s)",
                  os.environ.get("DA_DB_PATH", "default"))
        return True
