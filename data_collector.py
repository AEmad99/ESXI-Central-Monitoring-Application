import ssl
import re
import platform
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import requests
from pyVim import connect
from pyVmomi import vim, vmodl
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

import database
from models import ESXiHost, HostMetrics, VM, NetworkDevice, IPLease, HistoryLog, IPStatus

# Disable SSL warnings
requests.packages.urllib3.disable_warnings()

# Lock to prevent concurrent subnet scans
_scan_lock = threading.Lock()

def is_scan_running():
    """Check if a subnet scan is currently in progress."""
    return _scan_lock.locked()

# --- Helper Functions ---

def format_guest_id(guest_id):
    """Formats a guestId string into a more readable OS name."""
    if not guest_id:
        return "Unknown"
    
    if "coreos" in guest_id.lower():
        return "CoreOS"

    guest_id = guest_id.replace("Guest", "")
    bitness = ""
    if guest_id.endswith("64"):
        bitness = " (64-bit)"
        guest_id = guest_id[:-2]
    elif guest_id.endswith("32"):
        bitness = " (32-bit)"
        guest_id = guest_id[:-2]
    
    guest_id = guest_id.replace("_", "")
    match = re.match(r'([a-zA-Z]+)(\d+)', guest_id)
    if match:
        os_name = match.group(1).upper()
        os_version = match.group(2)
        if os_name == "WINDOWS":
            os_name = "Windows"
        return f"{os_name} {os_version}{bitness}"
    
    return f"{guest_id.capitalize()}{bitness}"

def connect_host(host, user, password):
    """Establishes a connection to an ESXi host."""
    context = None
    if hasattr(ssl, '_create_unverified_context'):
        context = ssl._create_unverified_context()
    try:
        si = connect.SmartConnect(host=host, user=user, pwd=password, sslContext=context, disableSslCertValidation=True)
        return si
    except Exception as e:
        print(f"Failed to connect to {host}: {e}")
        return None

def _build_property_collector_spec(view_ref, property_list):
    """Builds a PropertySpec for the PropertyCollector."""
    obj_spec = vmodl.query.PropertyCollector.ObjectSpec()
    obj_spec.obj = view_ref
    obj_spec.skip = True

    traversal_spec = vmodl.query.PropertyCollector.TraversalSpec()
    traversal_spec.name = 'traverseEntities'
    traversal_spec.path = 'view'
    traversal_spec.skip = False
    traversal_spec.type = vim.view.ContainerView

    obj_spec.selectSet = [traversal_spec]

    prop_spec = vmodl.query.PropertyCollector.PropertySpec()
    prop_spec.type = vim.VirtualMachine
    prop_spec.pathSet = property_list

    filter_spec = vmodl.query.PropertyCollector.FilterSpec()
    filter_spec.objectSet = [obj_spec]
    filter_spec.propSet = [prop_spec]

    return filter_spec

# --- Data Collection Logic ---

def collect_host_data(host_id):
    """Collects metrics and VM data for a single host and updates the DB.

    Split into 3 phases to minimize DB lock duration:
    Phase 1: Read credentials (short DB session)
    Phase 2: Fetch all data from ESXi (NO DB session open)
    Phase 3: Write everything in one short transaction
    """
    # --- Phase 1: Read credentials from DB (short session) ---
    with database.SessionLocal() as db:
        host_row = db.query(ESXiHost).get(host_id)
        if not host_row:
            return
        ip = host_row.ip
        user = host_row.username
        password = host_row.password

    print(f"Collecting data for host: {ip}")
    si = connect_host(ip, user, password)

    if not si:
        print(f"Skipping {ip} due to connection failure.")
        return

    # --- Phase 2: Fetch all data from ESXi (NO DB session open) ---
    try:
        content = si.RetrieveContent()

        # 1. Host Metrics
        host_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
        esxi_host = host_view.view[0]
        host_summary = esxi_host.summary

        used_cpu_mhz = host_summary.quickStats.overallCpuUsage
        total_cpu_mhz = host_summary.hardware.cpuMhz * host_summary.hardware.numCpuThreads
        cpu_usage = round((used_cpu_mhz / total_cpu_mhz) * 100, 2) if total_cpu_mhz > 0 else 0

        total_memory_gb = round(host_summary.hardware.memorySize / (1024**3), 2)
        used_memory_gb = round(host_summary.quickStats.overallMemoryUsage / 1024, 2)
        mem_usage = round((used_memory_gb / total_memory_gb) * 100, 2) if total_memory_gb > 0 else 0

        total_storage_bytes = sum(ds.summary.capacity for ds in esxi_host.datastore)
        free_storage_bytes = sum(ds.summary.freeSpace for ds in esxi_host.datastore)
        total_storage_gb = round(total_storage_bytes / (1024**3), 2)
        used_storage_gb = round((total_storage_bytes - free_storage_bytes) / (1024**3), 2)
        storage_usage = round((used_storage_gb / total_storage_gb) * 100, 2) if total_storage_gb > 0 else 0

        # Collect metrics dict (written to DB in Phase 3)
        metrics_data = {
            "host_id": host_id,
            "cpu_usage": cpu_usage,
            "used_cpu_ghz": round(used_cpu_mhz / 1000, 2),
            "total_cpu_ghz": round(total_cpu_mhz / 1000, 2),
            "mem_usage": mem_usage,
            "used_mem_gb": used_memory_gb,
            "total_mem_gb": total_memory_gb,
            "storage_usage": storage_usage,
            "used_storage_gb": used_storage_gb,
            "total_storage_gb": total_storage_gb,
            "last_updated": datetime.now()
        }

        host_view.Destroy()

        # 2. VMs — collect into plain dicts
        vm_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        properties = [
            "name", "summary.config.name", "summary.guest.guestFullName", "summary.guest.guestId",
            "config.guestFullName", "config.guestId",
            "summary.guest.ipAddress", "guest.net", "summary.config.memorySizeMB", "summary.quickStats.guestMemoryUsage",
            "summary.config.numCpu", "summary.quickStats.overallCpuUsage",
            "config.hardware.device", "config.createDate", "runtime.powerState"
        ]
        host_cpu_mhz_per_thread = host_summary.hardware.cpuMhz
        filter_spec = _build_property_collector_spec(vm_view, properties)
        options = vmodl.query.PropertyCollector.RetrieveOptions()
        result = content.propertyCollector.RetrievePropertiesEx([filter_spec], options)

        collected_vms = []  # List of dicts with all VM data

        def process_object_content(objects):
            for obj_content in objects:
                vm_props = {prop.name: prop.val for prop in obj_content.propSet}

                config_name = vm_props.get("summary.config.name", "Unknown")

                guest_full_name = vm_props.get("summary.guest.guestFullName") or vm_props.get("config.guestFullName")
                guest_id = vm_props.get("summary.guest.guestId") or vm_props.get("config.guestId")

                # Extract ALL IPs
                guest_net = vm_props.get("guest.net", [])
                ip_list = []
                if guest_net:
                    for nic in guest_net:
                        if nic.ipConfig and nic.ipConfig.ipAddress:
                            for ip_entry in nic.ipConfig.ipAddress:
                                addr = ip_entry.ipAddress
                                if "." in addr and not addr.startswith("127."):
                                    ip_list.append(addr)

                if not ip_list:
                    summary_ip = vm_props.get("summary.guest.ipAddress")
                    if summary_ip:
                        ip_list.append(summary_ip)

                ip_address = ", ".join(sorted(set(ip_list))) if ip_list else "N/A"

                create_date = vm_props.get("config.createDate")
                power_state = str(vm_props.get("runtime.powerState", "Unknown"))

                # OS Name Logic
                os_name = "Unknown"
                if guest_full_name:
                    os_name = str(guest_full_name)
                elif guest_id:
                    os_name = format_guest_id(str(guest_id))

                # RAM
                total_ram = vm_props.get("summary.config.memorySizeMB", 0)
                used_ram = vm_props.get("summary.quickStats.guestMemoryUsage", 0)
                ram_perc = round((used_ram / total_ram) * 100, 1) if total_ram > 0 else 0
                ram_str = f"{used_ram} / {total_ram} MB ({ram_perc}%)"

                # Disks
                devices = vm_props.get("config.hardware.device", [])
                disk_details = []
                disk_total_gb = 0.0
                try:
                    for device in devices:
                        if isinstance(device, vim.vm.device.VirtualDisk):
                            disk_label = device.deviceInfo.label
                            capacity_gb = round(device.capacityInKB / (1024 * 1024), 2)
                            disk_total_gb += capacity_gb
                            disk_details.append(f"{disk_label} ({capacity_gb}GB)")
                except: pass
                disks_str = ", ".join(disk_details) if disk_details else "N/A"
                disk_total_gb = round(disk_total_gb, 2)

                # VM CPU usage
                vm_cpu_usage_mhz = vm_props.get("summary.quickStats.overallCpuUsage", 0) or 0
                num_cpu = vm_props.get("summary.config.numCpu", 0) or 0
                vm_cpu_total_mhz = num_cpu * host_cpu_mhz_per_thread
                vm_cpu_usage_pct = round((vm_cpu_usage_mhz / vm_cpu_total_mhz) * 100, 1) if vm_cpu_total_mhz > 0 else 0

                created_date_str = create_date.isoformat() if isinstance(create_date, datetime) else None

                collected_vms.append({
                    "name": config_name,
                    "os": os_name,
                    "ip": ip_address,
                    "cpu_count": num_cpu,
                    "cpu_usage_mhz": vm_cpu_usage_mhz,
                    "cpu_total_mhz": vm_cpu_total_mhz,
                    "cpu_usage": vm_cpu_usage_pct,
                    "ram_used_mb": used_ram,
                    "ram_total_mb": total_ram,
                    "ram_usage": ram_perc,
                    "ram_info": ram_str,
                    "disk_total_gb": disk_total_gb,
                    "disk_info": disks_str,
                    "created_date": created_date_str,
                    "power_state": power_state,
                })

        if result:
            process_object_content(result.objects)
            token = result.token
            while token:
                result = content.propertyCollector.ContinueRetrievePropertiesEx(token)
                process_object_content(result.objects)
                token = result.token

        vm_view.Destroy()

    except Exception as e:
        print(f"Error fetching ESXi data for host {ip}: {e}")
        return
    finally:
        connect.Disconnect(si)

    # --- Phase 3: Write everything to DB in one short transaction ---
    with database.SessionLocal() as db:
        try:
            # Replace host metrics
            db.query(HostMetrics).filter_by(host_id=host_id).delete()
            db.add(HostMetrics(**metrics_data))

            # Fetch existing VMs for IP persistence + power state diff
            existing_vms = db.query(VM).filter_by(host_id=host_id).all()
            existing_ip_map = {vm.name: vm.ip for vm in existing_vms}

            # Delete + re-insert all VMs
            db.query(VM).filter_by(host_id=host_id).delete()

            for vm_data in collected_vms:
                # IP persistence for offline VMs
                if vm_data["ip"] == "N/A":
                    cached_ip = existing_ip_map.get(vm_data["name"])
                    if cached_ip and cached_ip != "N/A":
                        print(f"Using cached IP {cached_ip} for offline VM {vm_data['name']}")
                        vm_data["ip"] = cached_ip

                new_vm = VM(
                    host_id=host_id,
                    last_updated=datetime.now(),
                    **vm_data
                )
                db.add(new_vm)

                # Sync with NetworkDevice and IPLease
                if vm_data["ip"] and vm_data["ip"] != "N/A":
                    for single_ip in vm_data["ip"].split(", "):
                        device = db.query(NetworkDevice).filter_by(hostname=vm_data["name"]).first()
                        if not device:
                            device = NetworkDevice(hostname=vm_data["name"], first_seen=datetime.now())
                            db.add(device)

                        device.last_seen = datetime.now()

                        subnet_prefix = ".".join(single_ip.split(".")[:3])

                        lease = db.query(IPLease).get(single_ip)
                        new_lease_status = IPStatus.ACTIVE if "poweredOn" in vm_data["power_state"] else IPStatus.RESERVED

                        has_history = db.query(HistoryLog.id).filter_by(ip=single_ip).first() is not None

                        if not lease or lease.status != new_lease_status or not has_history:
                            log = HistoryLog(
                                timestamp=datetime.now(),
                                ip=single_ip,
                                status=new_lease_status,
                                device_id=device.id,
                                hostname_snapshot=device.hostname
                            )
                            db.add(log)

                        if not lease:
                            lease = IPLease(ip=single_ip, subnet=subnet_prefix, status=new_lease_status, device=device)
                            db.add(lease)
                        else:
                            lease.device = device
                            lease.status = new_lease_status
                            lease.last_updated = datetime.now()

            db.commit()
            print(f"Updated data for host {ip}")

        except Exception as e:
            db.rollback()
            print(f"Error writing data for host {ip}: {e}")

# --- Network Scanning Logic ---

import socket

def check_port(ip, port, timeout=0.5):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except:
        return False

def scan_ip(ip):
    """Pings an IP address. If ping fails, attempts TCP handshake on common ports."""
    # Stage 1: ICMP Ping
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    timeout_param = '-w' if platform.system().lower() == 'windows' else '-W'
    timeout_val = '500' if platform.system().lower() == 'windows' else '1' # 500ms or 1s
    
    command = ['ping', param, '1', timeout_param, timeout_val, ip]
    try:
        subprocess.check_call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ip, True, "ICMP"
    except subprocess.CalledProcessError:
        # Stage 2: DNS Check (Port 53)
        # If Port 53 is open, it's definitely a DNS server (UP), even if it blocks ICMP
        if check_port(ip, 53, timeout=0.3):
             return ip, True, "DNS:53"

        # Stage 3: General Fallback
        # Common management ports: 3389 (RDP), 445 (SMB), 22 (SSH), 5985 (WinRM HTTP)
        fallback_ports = [3389, 445, 22, 5985]
        for port in fallback_ports:
            if check_port(ip, port, timeout=0.3):
                return ip, True, f"TCP:{port}"
        
        return ip, False, None

def scan_and_store_subnet(subnet_prefix):
    """Scans a subnet and updates the DB with smart logic."""
    print(f"Scanning subnet {subnet_prefix}.0/24...")
    ips_to_scan = [f"{subnet_prefix}.{i}" for i in range(256)]
    
    # Reduced workers to prevent CPU starvation
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(scan_ip, ips_to_scan))
    
    with database.SessionLocal() as db:
        try:
            # Pre-fetch existing IPs to avoid N+1 queries for history check
            # We need to know if we should write history
            existing_logs_query = db.query(HistoryLog.ip).filter(HistoryLog.ip.in_(ips_to_scan)).group_by(HistoryLog.ip)
            existing_log_ips = {row[0] for row in existing_logs_query.all()}

            # Bulk pre-fetch all leases for this subnet to avoid N+1 queries
            # Using joinedload for device to avoid N+1 when accessing lease.device
            existing_leases = db.query(IPLease).options(joinedload(IPLease.device)).filter_by(subnet=subnet_prefix).all()
            lease_map = {lease.ip: lease for lease in existing_leases}

            for ip, is_active, protocol in results:
                lease = lease_map.get(ip)
                
                # Helper to get current status
                current_status = lease.status if lease else IPStatus.FREE
                
                new_status = IPStatus.FREE
                if is_active:
                    new_status = IPStatus.ACTIVE
                    # Update device type if DNS detected
                    if protocol and "DNS" in protocol:
                        # Find or create device for this IP
                        if lease and lease.device:
                             lease.device.type = "DNS Server"
                        else:
                             # Create a new device placeholder if none exists
                             new_device = NetworkDevice(
                                 hostname=f"DNS-{ip}",
                                 type="DNS Server",
                                 first_seen=datetime.now(),
                                 last_seen=datetime.now()
                             )
                             db.add(new_device)
                             # Link lease to this new device
                             if lease:
                                 lease.device = new_device
                else:
                    # Smart Logic: If it was Active or Reserved, keep it Reserved/Down
                    if current_status in [IPStatus.ACTIVE, IPStatus.RESERVED]:
                        new_status = IPStatus.RESERVED
                    elif current_status == IPStatus.DOWN:
                        new_status = IPStatus.DOWN
                    else:
                        new_status = IPStatus.FREE
                
                # Check if history missing
                has_history = ip in existing_log_ips
                
                # If status changed OR no history exists, log it (Time Travel Baseline)
                if not lease or lease.status != new_status or not has_history:
                    
                    # Log History
                    log = HistoryLog(
                        timestamp=datetime.now(),
                        ip=ip,
                        status=new_status,
                        device_id=lease.device_id if lease else None,
                        hostname_snapshot=lease.device.hostname if lease and lease.device else None
                    )
                    db.add(log)
                
                # Update/Create Lease
                if not lease:
                    lease = IPLease(ip=ip, subnet=subnet_prefix, status=new_status, last_updated=datetime.now())
                    db.add(lease)
                else:
                    lease.status = new_status
                    lease.last_updated = datetime.now()
            
            db.commit()
            print(f"Finished scanning {subnet_prefix}.0/24")
        except Exception as e:
            print(f"Error scanning subnet: {e}")
            db.rollback()

def scan_all_subnets():
    """Scans all subnets defined in the database. Uses a lock to prevent concurrent scans."""
    if not _scan_lock.acquire(blocking=False):
        print("Subnet scan already in progress, skipping.")
        return
    try:
        subnets = database.get_all_subnets()
        print(f"Starting bulk scan for {len(subnets)} subnets...")
        for subnet in subnets:
            scan_and_store_subnet(subnet)
        print("Bulk subnet scan completed.")
    finally:
        _scan_lock.release()

# --- Main Update Function ---

def update_all_hosts():
    """Fetches all hosts from DB and triggers collection for them."""
    with database.SessionLocal() as db:
        host_ids = [h.id for h in db.query(ESXiHost).all()]

    # Reduced workers for host data collection
    with ThreadPoolExecutor(max_workers=4) as executor:
        executor.map(collect_host_data, host_ids)

def update_specific_subnet(subnet):
    scan_and_store_subnet(subnet)

if __name__ == "__main__":
    database.init_db()
    
    # Check for empty hosts to seed based on existing logic preference
    # ideally we call seed logic from database.py but user might expect it here
    # update_all_hosts()
