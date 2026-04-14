import threading
import asyncio
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import data_collector
import database
from models import ESXiHost

# ---------------------------------------------------------------------------
# Collector thread state
# ---------------------------------------------------------------------------

_collector_lock = threading.Lock()
_collector_thread = None
_collector_status = {
    "running": False,
    "collecting": False,
    "last_run": None,
    "last_network_run": None,
    "next_run": None,
    "last_error": None,
    "stop_event": threading.Event(),
}

DEFAULT_INTERVAL = 1800       # 30 minutes — full host/VM collection
NETWORK_SCAN_INTERVAL = 21600 # 6 hours  — full IPAM subnet scan

# ---------------------------------------------------------------------------
# Event watcher registry  (one VMEventWatcher thread per ESXi host)
# ---------------------------------------------------------------------------

_watcher_lock = threading.Lock()
_event_watchers: dict[int, data_collector.VMEventWatcher] = {}  # host_id → watcher

# Per-host on-demand probe tracking
_host_probe_threads: dict[str, threading.Thread] = {}
_host_probe_lock = threading.Lock()


def _sync_event_watchers():
    """
    Ensure exactly one live VMEventWatcher exists for every ESXi host.
    Called after each full collection cycle so newly-added hosts are picked up
    and crashed watchers are restarted automatically.
    """
    with database.SessionLocal() as db:
        host_ids = [h.id for h in db.query(ESXiHost).all()]

    with _watcher_lock:
        for host_id in host_ids:
            existing = _event_watchers.get(host_id)
            if existing is None or not existing.is_alive():
                watcher = data_collector.VMEventWatcher(host_id)
                watcher.start()
                _event_watchers[host_id] = watcher
                print(f"[{datetime.now()}] EventWatcher started for host_id={host_id}")

        # Remove watchers for hosts that no longer exist
        stale = [hid for hid in _event_watchers if hid not in host_ids]
        for hid in stale:
            _event_watchers[hid].stop()
            del _event_watchers[hid]


def _stop_all_event_watchers():
    with _watcher_lock:
        for watcher in _event_watchers.values():
            watcher.stop()
        _event_watchers.clear()


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------

def get_interval_seconds() -> int:
    """Read the collection interval from DB settings, fallback to default."""
    try:
        val = database.get_setting("collection_interval_seconds")
        return int(val) if val else DEFAULT_INTERVAL
    except Exception:
        return DEFAULT_INTERVAL


def set_interval_seconds(seconds: int):
    """Persist a new collection interval and wake the sleeping thread."""
    database.set_setting("collection_interval_seconds", str(seconds))
    _collector_status["stop_event"].set()


def get_status() -> dict:
    return {
        "running":    _collector_status["running"],
        "collecting": _collector_status["collecting"],
        "last_run":   _collector_status["last_run"],
        "next_run":   _collector_status["next_run"],
        "last_error": _collector_status["last_error"],
    }


# ---------------------------------------------------------------------------
# Collection cycle
# ---------------------------------------------------------------------------

async def _async_collection_cycle(force_network: bool = False):
    """
    Run one full data-collection cycle.
    • All ESXi hosts are collected concurrently via ThreadPoolExecutor.
    • An IPAM subnet scan runs alongside if its interval has elapsed.
    """
    _collector_status["collecting"] = True
    _collector_status["last_error"] = None
    try:
        # Reload data_collector from disk before every cycle so that code
        # changes applied while the process is running take effect immediately
        # without requiring a full process restart.
        import importlib
        importlib.reload(data_collector)

        now = datetime.now()

        should_scan_network = force_network
        last_net = _collector_status["last_network_run"]
        if not last_net or (now - last_net).total_seconds() >= NETWORK_SCAN_INTERVAL:
            should_scan_network = True

        print(f"[{now}] Backend sync starting — network scan: {should_scan_network}")

        with database.SessionLocal() as db:
            host_ids = [h.id for h in db.query(ESXiHost).all()]

        # All hosts collected truly in parallel; subnet scan runs alongside
        with ThreadPoolExecutor(max_workers=max(len(host_ids), 1) + 2) as executor:
            loop = asyncio.get_running_loop()
            host_tasks = [
                loop.run_in_executor(executor, data_collector.collect_host_data, hid)
                for hid in host_ids
            ]
            if should_scan_network:
                await asyncio.gather(*host_tasks, data_collector.async_scan_all_subnets())
                _collector_status["last_network_run"] = now
            else:
                await asyncio.gather(*host_tasks)

        _collector_status["last_run"] = now
        # Signal the dashboard that fresh bulk data is available
        database.set_setting("last_collection_ts", now.isoformat())
        print(f"[{datetime.now()}] Backend sync completed.")

    except Exception as e:
        _collector_status["last_error"] = str(e)
        print(f"[{datetime.now()}] Sync error: {e}")
    finally:
        _collector_status["collecting"] = False


# ---------------------------------------------------------------------------
# Collector loop
# ---------------------------------------------------------------------------

def _collector_loop():
    """
    Main loop for the background collector thread.

    Flow per iteration:
      1. Run a full collection cycle (all hosts in parallel).
      2. Sync event-watcher threads (start/restart as needed).
      3. Sleep for the configured interval, waking early on signals.
    """
    _collector_status["running"] = True
    stop_event = _collector_status["stop_event"]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    first_run = True
    while _collector_status["running"]:
        # --- Full collection ---
        loop.run_until_complete(_async_collection_cycle())

        # --- Sync event watchers after first (and every subsequent) collection ---
        # Watchers start after first collection so all VMs exist in DB already.
        _sync_event_watchers()
        if first_run:
            first_run = False
            print(f"[{datetime.now()}] Real-time event watchers initialised.")

        # --- Sleep until next cycle ---
        interval = get_interval_seconds()
        _collector_status["next_run"] = datetime.now() + timedelta(seconds=interval)
        signaled = stop_event.wait(timeout=interval)

        if signaled:
            stop_event.clear()
            if not _collector_status["running"]:
                break
            # Interval changed or manual trigger — loop immediately
            continue

    _collector_status["running"] = False
    _collector_status["next_run"] = None
    _stop_all_event_watchers()
    loop.close()
    print(f"[{datetime.now()}] Background collector stopped.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start():
    """Start the background collector + event-watcher system (idempotent)."""
    global _collector_thread
    with _collector_lock:
        if _collector_thread is not None and _collector_thread.is_alive():
            return
        _collector_status["running"] = True
        _collector_status["stop_event"].clear()
        _collector_thread = threading.Thread(
            target=_collector_loop, daemon=True, name="bg-collector"
        )
        _collector_thread.start()
        print(f"[{datetime.now()}] Background collector thread started.")


def stop():
    """Signal the collector and all event-watcher threads to stop."""
    global _collector_thread
    with _collector_lock:
        _collector_status["running"] = False
        _collector_status["stop_event"].set()
        _collector_thread = None


def is_running() -> bool:
    return _collector_thread is not None and _collector_thread.is_alive()


def trigger_now():
    """Wake the background collector to run a full cycle immediately.
    No-op if a collection is already in progress (it will run again right after)."""
    _collector_status["stop_event"].set()


def trigger_host_probe(host_ip: str):
    """Start a non-blocking targeted data probe for a single ESXi host.
    Silently skipped if a probe for this host is already running."""
    def _run():
        try:
            import importlib
            importlib.reload(data_collector)
            data_collector.update_single_host_by_ip(host_ip)
            database.set_setting("last_collection_ts", datetime.now().isoformat())
        except Exception as e:
            print(f"[{datetime.now()}] On-demand probe failed for {host_ip}: {e}")

    with _host_probe_lock:
        existing = _host_probe_threads.get(host_ip)
        if existing and existing.is_alive():
            return
        t = threading.Thread(target=_run, daemon=True, name=f"probe-{host_ip}")
        t.start()
        _host_probe_threads[host_ip] = t


def is_host_probing(host_ip: str) -> bool:
    """Return True if an on-demand probe is currently running for this host."""
    with _host_probe_lock:
        t = _host_probe_threads.get(host_ip)
        return t is not None and t.is_alive()
