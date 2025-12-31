import time
from datetime import datetime
import data_collector
import db_manager

# Configuration
UPDATE_INTERVAL_SECONDS = 3600  # 1 Hour

def job():
    print(f"[{datetime.now()}] Starting scheduled background update...")
    try:
        data_collector.update_all_hosts()
        print(f"[{datetime.now()}] Update completed successfully.")
    except Exception as e:
        print(f"[{datetime.now()}] Update failed: {e}")

if __name__ == "__main__":
    print(f"Starting background job scheduler. Updating every {UPDATE_INTERVAL_SECONDS} seconds.")
    
    # Ensure DB is ready
    db_manager.init_db()
    
    # Run once on startup
    job()

    while True:
        # Sleep for the interval
        time.sleep(UPDATE_INTERVAL_SECONDS)
        job()