from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import sessionmaker
from models import Base, ESXiHost, Subnet, AppSettings
import os

DB_FILE = 'monitoring.db'
DATABASE_URL = f"sqlite:///{DB_FILE}"

engine = create_engine(DATABASE_URL, echo=False, connect_args={"timeout": 30})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@event.listens_for(engine, "connect")
def _set_sqlite_wal(dbapi_conn, connection_record):
    """Enable WAL mode for concurrent read/write access."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()

def init_db():
    # create tables
    Base.metadata.create_all(bind=engine)
    # migrate existing tables to add new columns
    _migrate_vm_columns()
    # ensure indexes exist
    _create_indexes()

def _migrate_vm_columns():
    """Add numeric resource columns to vms table if they don't exist."""
    new_columns = [
        ("cpu_usage_mhz", "INTEGER"),
        ("cpu_total_mhz", "INTEGER"),
        ("cpu_usage", "REAL"),
        ("ram_used_mb", "INTEGER"),
        ("ram_total_mb", "INTEGER"),
        ("ram_usage", "REAL"),
        ("disk_total_gb", "REAL"),
    ]
    with engine.connect() as conn:
        for col_name, col_type in new_columns:
            try:
                with conn.begin():
                    conn.execute(text(f"ALTER TABLE vms ADD COLUMN {col_name} {col_type}"))
            except Exception:
                pass  # column already exists

def _create_indexes():
    """Ensure all required indexes exist in the database."""
    indexes = [
        ("ix_vms_name", "vms", "name"),
        ("ix_vms_ip", "vms", "ip"),
        ("ix_vms_power_state", "vms", "power_state"),
        ("ix_vms_created_date", "vms", "created_date"),
        ("ix_esxi_hosts_group_name", "esxi_hosts", "group_name"),
        ("ix_network_devices_hostname", "network_devices", "hostname"),
    ]
    with engine.connect() as conn:
        for idx_name, table, col in indexes:
            try:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col})"))
            except Exception:
                pass
        conn.commit()

def get_session():
    db = SessionLocal()
    return db

# Validation/Seed functions
def seed_subnets_if_empty(default_range=15):
    db = get_session()
    try:
        if db.query(Subnet).count() == 0:
            print("Seeding database with initial subnets...")
            for i in range(default_range):
                prefix = f"192.168.{i}"
                if not db.query(Subnet).filter_by(prefix=prefix).first():
                    db.add(Subnet(prefix=prefix))
            db.commit()
    finally:
        db.close()

def update_hosts_from_config(host_groups, default_user="root"):
    db = get_session()
    try:
        print("Syncing host configuration to database...")
        # Get all current IPs in DB to know what to remove? 
        # For now, just upsert as per old logic.
        
        for group_name, group_data in host_groups.items():
            password = group_data["pass"]
            user = group_data.get("user", default_user)
            for ip in group_data["ips"]:
                host = db.query(ESXiHost).filter_by(ip=ip).first()
                if host:
                    host.username = user
                    host.password = password
                    # host.group_name = group_name # If we added this column
                else:
                    new_host = ESXiHost(ip=ip, username=user, password=password, group_name=group_name)
                    db.add(new_host)
        db.commit()
    finally:
        db.close()

def seed_hosts_if_empty(host_groups, default_user="root"):
    # This overlaps with update_hosts_from_config but was separate in old code
    # We can just call update
    update_hosts_from_config(host_groups, default_user)

# Helper to get all subnets as list of strings (legacy compat)
def get_all_subnets():
    db = get_session()
    subnets = db.query(Subnet).all()
    db.close()
    return [s.prefix for s in subnets]

def add_subnet(prefix):
    db = get_session()
    try:
        if db.query(Subnet).filter_by(prefix=prefix).first():
            return False
        db.add(Subnet(prefix=prefix))
        db.commit()
        return True
    except:
        return False
    finally:
        db.close()

def remove_subnet(prefix):
    db = get_session()
    try:
        db.query(Subnet).filter_by(prefix=prefix).delete()
        # Also clean scans? The logic for IPLease might be different now.
        # old: conn.execute('DELETE FROM network_scans WHERE subnet = ?', (prefix,))
        # new: delete IPLeases for this subnet
        from models import IPLease
        db.query(IPLease).filter_by(subnet=prefix).delete()
        db.commit()
    finally:
        db.close()

# --- App Settings ---

def get_setting(key, default=None):
    db = get_session()
    try:
        row = db.query(AppSettings).filter_by(key=key).first()
        return row.value if row else default
    finally:
        db.close()

def set_setting(key, value):
    db = get_session()
    try:
        row = db.query(AppSettings).filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            db.add(AppSettings(key=key, value=str(value)))
        db.commit()
    finally:
        db.close()
