import ssl
import re
import platform
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import requests
from pyVim import connect
from pyVmomi import vim, vmodl
import db_manager

# Disable SSL warnings
requests.packages.urllib3.disable_warnings()

# --- Helper Functions (Ported from monitoring_dashboard.py) ---

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

def collect_host_data(host_row):
    """Collects metrics and VM data for a single host and updates the DB."""
    host_id = host_row['id']
    ip = host_row['ip']
    user = host_row['username']
    password = host_row['password']

    print(f"Collecting data for host: {ip}")
    si = connect_host(ip, user, password)
    
    conn = db_manager.get_db_connection()
    c = conn.cursor()

    if not si:
        print(f"Skipping {ip} due to connection failure.")
        # Optionally mark host as down in DB? For now, we just don't update metrics.
        conn.close()
        return

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

        # Insert/Update Metrics (We keep history? For now, let's just insert a new record or update latest. 
        # The prompt implies 'dashboard' view, so latest is key, but 'database' implies history. 
        # I'll DELETE old metrics for this host to keep it lightweight as requested ("lightweight database"), 
        # or we can keep them. Let's keep only the latest entry for now to mimic the current state behavior.)
        c.execute("DELETE FROM host_metrics WHERE host_id = ?", (host_id,))
        c.execute('''
            INSERT INTO host_metrics (
                host_id, cpu_usage, used_cpu_ghz, total_cpu_ghz, 
                mem_usage, used_mem_gb, total_mem_gb, 
                storage_usage, used_storage_gb, total_storage_gb, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            host_id, cpu_usage, round(used_cpu_mhz / 1000, 2), round(total_cpu_mhz / 1000, 2),
            mem_usage, used_memory_gb, total_memory_gb,
            storage_usage, used_storage_gb, total_storage_gb, datetime.now()
        ))
        
        host_view.Destroy()

        # 2. VMs
        vm_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        properties = [
            "name", "summary.config.name", "summary.guest.guestFullName", "summary.guest.guestId",
            "config.guestFullName", "config.guestId",
            "summary.guest.ipAddress", "guest.net", "summary.config.memorySizeMB", "summary.quickStats.guestMemoryUsage",
            "summary.config.numCpu", "config.hardware.device", "config.createDate", "runtime.powerState"
        ]
        filter_spec = _build_property_collector_spec(vm_view, properties)
        options = vmodl.query.PropertyCollector.RetrieveOptions()
        result = content.propertyCollector.RetrievePropertiesEx([filter_spec], options)

        # Custom Logic: specific persistence for offline VMs
        # Fetch existing IPs for this host to preserve them if VM is powered off
        current_db_vms = conn.execute("SELECT name, ip FROM vms WHERE host_id = ?", (host_id,)).fetchall()
        existing_ip_map = {row['name']: row['ip'] for row in current_db_vms}

        # Clear old VMs for this host before inserting new snapshot
        c.execute("DELETE FROM vms WHERE host_id = ?", (host_id,))

        def process_object_content(objects):
            for obj_content in objects:
                vm_props = {prop.name: prop.val for prop in obj_content.propSet}
                
                config_name = vm_props.get("summary.config.name", "Unknown")
                
                # Guest OS Resolution Priority:
                # 1. summary.guest.guestFullName (Tools reported, most accurate)
                # 2. config.guestFullName (Configured in VM Settings)
                guest_full_name = vm_props.get("summary.guest.guestFullName")
                if not guest_full_name:
                    guest_full_name = vm_props.get("config.guestFullName")
                
                # 3. summary.guest.guestId (Tools reported ID)
                # 4. config.guestId (Configured ID)
                guest_id = vm_props.get("summary.guest.guestId")
                if not guest_id:
                    guest_id = vm_props.get("config.guestId")
                
                # Extract ALL IPs from guest.net
                guest_net = vm_props.get("guest.net", [])
                ip_list = []
                if guest_net:
                    for nic in guest_net:
                        if nic.ipConfig and nic.ipConfig.ipAddress:
                            for ip_entry in nic.ipConfig.ipAddress:
                                ip = ip_entry.ipAddress
                                # Filter for IPv4 (simple check) and ignore localhost
                                if "." in ip and not ip.startswith("127."):
                                    ip_list.append(ip)
                
                # Fallback to summary IP if net property is empty/missing
                if not ip_list:
                    summary_ip = vm_props.get("summary.guest.ipAddress")
                    if summary_ip:
                        ip_list.append(summary_ip)
                
                # Join unique IPs
                ip_address = ", ".join(sorted(set(ip_list))) if ip_list else "N/A"
                
                # Persistence Check: If IP is N/A, check our cache
                if ip_address == "N/A":
                    cached_ip = existing_ip_map.get(config_name)
                    if cached_ip and cached_ip != "N/A":
                        print(f"Using cached IP {cached_ip} for offline VM {config_name}")
                        ip_address = cached_ip

                create_date = vm_props.get("config.createDate")
                power_state = vm_props.get("runtime.powerState", "Unknown")

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
                try:
                    for device in devices:
                        if isinstance(device, vim.vm.device.VirtualDisk):
                            disk_label = device.deviceInfo.label
                            capacity_gb = round(device.capacityInKB / (1024 * 1024), 2)
                            disk_details.append(f"{disk_label} ({capacity_gb}GB)")
                except: pass
                disks_str = ", ".join(disk_details) if disk_details else "N/A"

                # Format Date
                created_date_str = None
                if isinstance(create_date, datetime):
                    created_date_str = create_date.isoformat()
                
                c.execute('''
                    INSERT INTO vms (
                        host_id, name, os, ip, cpu_count, ram_info, disk_info, created_date, power_state, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    host_id, config_name, os_name, ip_address, vm_props.get("summary.config.numCpu", 0),
                    ram_str, disks_str, created_date_str, str(power_state), datetime.now()
                ))

        if result:
            process_object_content(result.objects)
            token = result.token
            while token:
                result = content.propertyCollector.ContinueRetrievePropertiesEx(token)
                process_object_content(result.objects)
                token = result.token

        vm_view.Destroy()
        conn.commit()
        print(f"Updated data for host {ip}")

    except Exception as e:
        print(f"Error collecting data for host {ip}: {e}")
    finally:
        connect.Disconnect(si)
        conn.close()

# --- Network Scanning Logic ---

def scan_ip(ip):
    """Pings an IP address. Returns (ip, is_active)."""
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    timeout_param = '-w' if platform.system().lower() == 'windows' else '-W'
    timeout_val = '500' if platform.system().lower() == 'windows' else '1'
    
    command = ['ping', param, '1', timeout_param, timeout_val, ip]
    try:
        subprocess.check_call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ip, True
    except subprocess.CalledProcessError:
        return ip, False

def scan_and_store_subnet(subnet_prefix):
    """Scans a subnet and updates the DB."""
    print(f"Scanning subnet {subnet_prefix}.0/24...")
    ips_to_scan = [f"{subnet_prefix}.{i}" for i in range(256)]
    
    with ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(scan_ip, ips_to_scan))
    
    conn = db_manager.get_db_connection()
    c = conn.cursor()
    
    # Upsert results
    for ip, is_active in results:
        status = 'taken' if is_active else 'free'
        c.execute('''
            INSERT OR REPLACE INTO network_scans (subnet, ip, status, last_updated)
            VALUES (?, ?, ?, ?)
        ''', (subnet_prefix, ip, status, datetime.now()))
        
    conn.commit()
    conn.close()
    print(f"Finished scanning {subnet_prefix}.0/24")

def scan_all_subnets():
    """Scans all subnets defined in the database."""
    subnets = db_manager.get_all_subnets()
    print(f"Starting bulk scan for {len(subnets)} subnets...")
    for subnet in subnets:
        scan_and_store_subnet(subnet)
    print("Bulk subnet scan completed.")

# --- Main Update Function ---

def update_all_hosts():
    """Fetches all hosts from DB and triggers collection for them."""
    conn = db_manager.get_db_connection()
    hosts = conn.execute("SELECT * FROM hosts").fetchall()
    conn.close()

    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(collect_host_data, hosts)

def update_specific_subnet(subnet):
    scan_and_store_subnet(subnet)

if __name__ == "__main__":
    # If run directly, maybe perform a full update?
    # This is useful for testing.
    db_manager.init_db()
    # We assume DB is seeded by the main app or manual intervention for now if empty.
    update_all_hosts()
