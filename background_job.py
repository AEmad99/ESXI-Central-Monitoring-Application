import threading
import time
from datetime import datetime, timedelta
import data_collector
import database

# Shared state for the background collector thread (process-wide, not per-session)
_collector_lock = threading.Lock()
_collector_thread = None
_collector_status = {
    "running": False,
    "collecting": False,
    "last_run": None,
    "next_run": None,
    "last_error": None,
    "stop_event": threading.Event(),
}

DEFAULT_INTERVAL = 300  # 5 minutes

def get_interval_seconds():
    """Read the collection interval from DB settings, fallback to default."""
    try:
        val = database.get_setting("collection_interval_seconds")
        return int(val) if val else DEFAULT_INTERVAL
    except Exception:
        return DEFAULT_INTERVAL

def set_interval_seconds(seconds):
    """Persist the collection interval and wake the sleeping thread."""
    database.set_setting("collection_interval_seconds", str(seconds))
    # Wake the thread so it picks up the new interval immediately
    _collector_status["stop_event"].set()

def get_status():
    """Return a snapshot of the collector status."""
    return {
        "running": _collector_status["running"],
        "collecting": _collector_status["collecting"],
        "last_run": _collector_status["last_run"],
        "next_run": _collector_status["next_run"],
        "last_error": _collector_status["last_error"],
    }

def _collection_cycle():
    """Run one full data collection cycle."""
    _collector_status["collecting"] = True
    _collector_status["last_error"] = None
    try:
        print(f"[{datetime.now()}] Background collection started...")
        data_collector.update_all_hosts()
        print(f"[{datetime.now()}] Host update completed.")
        data_collector.scan_all_subnets()
        print(f"[{datetime.now()}] Subnet scan completed.")
        _collector_status["last_run"] = datetime.now()
    except Exception as e:
        _collector_status["last_error"] = str(e)
        print(f"[{datetime.now()}] Collection error: {e}")
    finally:
        _collector_status["collecting"] = False

def _collector_loop():
    """Main loop for the background collector thread."""
    _collector_status["running"] = True
    stop_event = _collector_status["stop_event"]

    # Run immediately on start
    _collection_cycle()

    while not stop_event.is_set():
        interval = get_interval_seconds()
        _collector_status["next_run"] = datetime.now() + timedelta(seconds=interval)

        # Sleep in small increments so we can respond to stop/interval changes
        stop_event.wait(timeout=interval)

        if stop_event.is_set():
            # Check if this is just an interval change (re-cleared below) or a real stop
            stop_event.clear()
            if not _collector_status["running"]:
                break
            # Interval changed — loop back to re-read interval and recalculate next_run
            continue

        _collection_cycle()

    _collector_status["running"] = False
    _collector_status["next_run"] = None
    print(f"[{datetime.now()}] Background collector stopped.")

def start():
    """Start the background collector thread (idempotent)."""
    global _collector_thread
    with _collector_lock:
        if _collector_thread is not None and _collector_thread.is_alive():
            return  # already running
        _collector_status["running"] = True
        _collector_status["stop_event"].clear()
        _collector_thread = threading.Thread(target=_collector_loop, daemon=True, name="bg-collector")
        _collector_thread.start()
        print(f"[{datetime.now()}] Background collector thread started.")

def stop():
    """Signal the background collector thread to stop."""
    global _collector_thread
    with _collector_lock:
        _collector_status["running"] = False
        _collector_status["stop_event"].set()
        _collector_thread = None

def is_running():
    """Check if the collector thread is alive."""
    return _collector_thread is not None and _collector_thread.is_alive()
