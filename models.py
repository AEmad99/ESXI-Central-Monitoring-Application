from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
import enum

Base = declarative_base()

class IPStatus(enum.Enum):
    FREE = 'Free'
    ACTIVE = 'Active'
    RESERVED = 'Reserved'
    DOWN = 'Down'

class ESXiHost(Base):
    __tablename__ = 'esxi_hosts'
    id = Column(Integer, primary_key=True)
    ip = Column(String, unique=True, nullable=False)
    username = Column(String, nullable=False)
    password = Column(String, nullable=False)
    group_name = Column(String, index=True)
    
    vms = relationship("VM", back_populates="esxi_host", cascade="all, delete-orphan")
    host_metrics = relationship("HostMetrics", back_populates="esxi_host", cascade="all, delete-orphan")

class HostMetrics(Base):
    __tablename__ = 'host_metrics'
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, ForeignKey('esxi_hosts.id'), nullable=False)
    cpu_usage = Column(Float)
    used_cpu_ghz = Column(Float)
    total_cpu_ghz = Column(Float)
    mem_usage = Column(Float)
    used_mem_gb = Column(Float)
    total_mem_gb = Column(Float)
    storage_usage = Column(Float)
    used_storage_gb = Column(Float)
    total_storage_gb = Column(Float)
    last_updated = Column(DateTime, default=datetime.now)
    
    esxi_host = relationship("ESXiHost", back_populates="host_metrics")

class VM(Base):
    __tablename__ = 'vms'
    id = Column(Integer, primary_key=True)
    host_id = Column(Integer, ForeignKey('esxi_hosts.id'), nullable=False)
    name = Column(String, nullable=False, index=True)
    os = Column(String)
    ip = Column(String, index=True)
    cpu_count = Column(Integer)
    cpu_usage_mhz = Column(Integer)
    cpu_total_mhz = Column(Integer)
    cpu_usage = Column(Float)
    ram_used_mb = Column(Integer)
    ram_total_mb = Column(Integer)
    ram_usage = Column(Float)
    ram_info = Column(String)
    disk_total_gb = Column(Float)
    disk_info = Column(String)
    created_date = Column(String, index=True)
    power_state = Column(String, index=True)
    last_updated = Column(DateTime, default=datetime.now)

    esxi_host = relationship("ESXiHost", back_populates="vms")

class NetworkDevice(Base):
    """
    Represents a unique machine on the network, identified by MAC or Hostname.
    Used for tracking IP ownership over time.
    """
    __tablename__ = 'network_devices'
    id = Column(Integer, primary_key=True)
    mac_address = Column(String, unique=True, nullable=True)
    hostname = Column(String, index=True)
    first_seen = Column(DateTime, default=datetime.now)
    last_seen = Column(DateTime, default=datetime.now)
    type = Column(String, default="Unknown") # 'VM', 'Physical', 'DNS Server', etc.
    
    ip_leases = relationship("IPLease", back_populates="device")
    history_logs = relationship("HistoryLog", back_populates="device")

class IPLease(Base):
    """
    Current state of an IP address.
    """
    __tablename__ = 'ip_leases'
    ip = Column(String, primary_key=True)
    subnet = Column(String, nullable=False, index=True)
    status = Column(Enum(IPStatus), default=IPStatus.FREE)
    device_id = Column(Integer, ForeignKey('network_devices.id'), nullable=True)
    last_updated = Column(DateTime, default=datetime.now)
    
    device = relationship("NetworkDevice", back_populates="ip_leases")

class HistoryLog(Base):
    """
    Time-series log of IP state changes.
    """
    __tablename__ = 'history_log'
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    ip = Column(String, nullable=False, index=True)
    status = Column(Enum(IPStatus), nullable=False)
    device_id = Column(Integer, ForeignKey('network_devices.id'), nullable=True)
    hostname_snapshot = Column(String)
    
    device = relationship("NetworkDevice", back_populates="history_logs")

class Subnet(Base):
    __tablename__ = 'subnets'
    prefix = Column(String, primary_key=True)

class AppSettings(Base):
    __tablename__ = 'app_settings'
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
