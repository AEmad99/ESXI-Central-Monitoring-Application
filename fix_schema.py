import sqlite3
import os

DB_FILE = "monitoring.db"

def fix_schema():
    if not os.path.exists(DB_FILE):
        print(f"Database {DB_FILE} not found.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    try:
        # Check columns in network_devices
        cursor.execute("PRAGMA table_info(network_devices)")
        columns = [info[1] for info in cursor.fetchall()]
        
        if 'type' not in columns:
            print("Missing 'type' column in network_devices. Adding it...")
            cursor.execute("ALTER TABLE network_devices ADD COLUMN type VARCHAR DEFAULT 'Unknown'")
            conn.commit()
            print("Schema updated successfully.")
        else:
            print("'type' column already exists.")
            
    except Exception as e:
        print(f"Error updating schema: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    fix_schema()
