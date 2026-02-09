from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, ESXiHost, Subnet
import os

DB_FILE = 'monitoring.db'
# Check if we are running in a specific environment, but default is local file
DATABASE_URL = f"sqlite:///{DB_FILE}"

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    # create tables
    Base.metadata.create_all(bind=engine)

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
