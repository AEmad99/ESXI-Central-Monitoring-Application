import threading
import time
import asyncio
from datetime import datetime, timedelta
import data_collector
import database
from models import ESXiHost

# Shared state for the background collector thread (process-wide, not per-session)
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

DEFAULT_INTERVAL = 120  # 2 minutes for host metrics
NETWORK_SCAN_INTERVAL = 3600 # 1 hour for full IPAM scan

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

async def _async_collection_cycle(force_network=False):
    """Run one full data collection cycle using the async engine."""
    _collector_status["collecting"] = True
    _collector_status["last_error"] = None
    try:
        # Determine if we should run a network scan (Slow Tier)
        now = datetime.now()
        should_scan_network = force_network
        if not _collector_status["last_network_run"] or \
           (now - _collector_status["last_network_run"]).total_seconds() >= NETWORK_SCAN_INTERVAL:
            should_scan_network = True

        print(f"[{now}] Backend sync starting (Infrastructure: True, Network: {should_scan_network})")
        
        # 1. Fetch host IDs
        with database.SessionLocal() as db:
            host_ids = [h.id for h in db.query(ESXiHost).all()]

        # 2. Parallel Infrastructure Update (Fast Tier)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(host_ids) + 2) as executor:
            loop = asyncio.get_running_loop()
            
            # Helper to run collection with a small initial delay to stagger logins
            async def staggered_collect(hid, delay):
                await asyncio.sleep(delay)
                return await loop.run_in_executor(executor, data_collector.collect_host_data, hid)

            # Create tasks with increasing delays (0.2s between each)
            host_tasks = [staggered_collect(hid, i * 0.2) for i, hid in enumerate(host_ids)]
            
            # Execute Infrastructure updates first or concurrently with networking
            if should_scan_network:
                await asyncio.gather(
                    *host_tasks,
                    data_collector.async_scan_all_subnets()
                )
                _collector_status["last_network_run"] = now
            else:
                await asyncio.gather(*host_tasks)
        
        _collector_status["last_run"] = now
        print(f"[{datetime.now()}] Backend sync completed.")
    except Exception as e:
        _collector_status["last_error"] = str(e)
        print(f"[{datetime.now()}] Sync error: {e}")
    finally:
        _collector_status["collecting"] = False

def _collector_loop():
    """Main loop for the background collector thread."""
    _collector_status["running"] = True
    stop_event = _collector_status["stop_event"]
    
    # We need a dedicated event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while _collector_status["running"]:
        # 1. Run the collection cycle
        loop.run_until_complete(_async_collection_cycle())

        # 2. Calculate next run
        interval = get_interval_seconds()
        _collector_status["next_run"] = datetime.now() + timedelta(seconds=interval)

        # 3. Wait for interval or signal (stop/trigger)
        signaled = stop_event.wait(timeout=interval)

        if signaled:
            stop_event.clear()
            if not _collector_status["running"]:
                break
            # If we were signaled but still running, it means an interval change occurred.
            # We loop immediately to start the next cycle.
            continue

    _collector_status["running"] = False
    _collector_status["next_run"] = None
    loop.close()
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
