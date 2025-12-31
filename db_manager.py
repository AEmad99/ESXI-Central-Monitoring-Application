import sqlite3
import json
import os
from datetime import datetime

DB_FILE = 'monitoring.db'

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # Hosts table (Stores config/credentials)
    c.execute('''
        CREATE TABLE IF NOT EXISTS hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            group_name TEXT
        )
    ''')

    # Host Metrics table
    c.execute('''
        CREATE TABLE IF NOT EXISTS host_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER,
            cpu_usage REAL,
            used_cpu_ghz REAL,
            total_cpu_ghz REAL,
            mem_usage REAL,
            used_mem_gb REAL,
            total_mem_gb REAL,
            storage_usage REAL,
            used_storage_gb REAL,
            total_storage_gb REAL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (host_id) REFERENCES hosts (id)
        )
    ''')

    # VMs table
    c.execute('''
        CREATE TABLE IF NOT EXISTS vms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER,
            name TEXT,
            os TEXT,
            ip TEXT,
            cpu_count INTEGER,
            ram_info TEXT,
            disk_info TEXT,
            created_date TEXT,
            power_state TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (host_id) REFERENCES hosts (id)
        )
    ''')

    # Network Scans table (replacing JSON cache)
    c.execute('''
        CREATE TABLE IF NOT EXISTS network_scans (
            subnet TEXT,
            ip TEXT,
            status TEXT, -- 'taken' or 'free'
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (subnet, ip)
        )
    ''')

    # Subnets configuration table
    c.execute('''
        CREATE TABLE IF NOT EXISTS subnets (
            prefix TEXT PRIMARY KEY
        )
    ''')

    conn.commit()
    conn.close()

def seed_hosts_if_empty(host_groups, default_user="root"):
    """
    Populates the hosts table from the hardcoded dictionary if the table is empty.
    """
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('SELECT count(*) FROM hosts')
    if c.fetchone()[0] == 0:
        print("Seeding database with initial host configuration...")
        for group_name, group_data in host_groups.items():
            password = group_data["pass"]
            for ip in group_data["ips"]:
                try:
                    c.execute('''
                        INSERT INTO hosts (ip, username, password, group_name)
                        VALUES (?, ?, ?, ?)
                    ''', (ip, default_user, password, group_name))
                except sqlite3.IntegrityError:
                    pass # Skip duplicates
        conn.commit()
    
    conn.close()

def seed_subnets_if_empty(default_range=15):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT count(*) FROM subnets')
    if c.fetchone()[0] == 0:
        print("Seeding database with initial subnets...")
        # Default 192.168.0.x to 192.168.14.x
        for i in range(default_range):
            try:
                c.execute('INSERT INTO subnets (prefix) VALUES (?)', (f"192.168.{i}",))
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    conn.close()

def get_all_subnets():
    conn = get_db_connection()
    rows = conn.execute('SELECT prefix FROM subnets ORDER BY prefix').fetchall()
    conn.close()
    return [row['prefix'] for row in rows]

def add_subnet(prefix):
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO subnets (prefix) VALUES (?)', (prefix,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def remove_subnet(prefix):
    conn = get_db_connection()
    conn.execute('DELETE FROM subnets WHERE prefix = ?', (prefix,))
    # Optionally clean up scan results for this subnet
    conn.execute('DELETE FROM network_scans WHERE subnet = ?', (prefix,))
    conn.commit()
    conn.close()
