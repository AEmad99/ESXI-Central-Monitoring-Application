import ssl
import re
import platform
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import requests
import asyncio
import aiohttp
import os
from pyVim import connect
from pyVmomi import vim, vmodl
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import database
from models import ESXiHost, HostMetrics, VM, NetworkDevice, IPLease, HistoryLog, IPStatus

# Disable SSL warnings
requests.packages.urllib3.disable_warnings()

# --- Alerting Dispatcher ---

class AlertDispatcher:
    """Manages threshold alerts and webhooks with a cooldown mechanism."""
    _cooldowns = {} # In-memory cache for alerts: {(host_ip, type): last_alert_time}

    @classmethod
    async def dispatch_webhook(cls, message):
        webhook_url = os.getenv("WEBHOOK_URL")
        if not webhook_url:
            return
            
        payload = {
            "text": f"🚨 *ESXi Dashboard Alert* 🚨\n{message}",
            "username": "Monitoring Bot"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=payload, timeout=5) as response:
                    if response.status not in (200, 204):
                        print(f"Webhook failed with status {response.status}")
        except Exception as e:
            print(f"Webhook error: {e}")

    @classmethod
    async def check_thresholds(cls, host_ip, cpu_usage, mem_usage, storage_usage):
        now = datetime.now()
        cooldown_period = timedelta(hours=1)
        
        thresholds = [
            ("CPU_HIGH", cpu_usage, 85, "CPU usage"),
            ("MEM_HIGH", mem_usage, 85, "Memory usage"),
            ("STORAGE_HIGH", storage_usage, 90, "Storage usage")
        ]
        
        for alert_type, value, limit, label in thresholds:
            if value and value > limit:
                key = (host_ip, alert_type)
                last_time = cls._cooldowns.get(key)
                
                if not last_time or (now - last_time) > cooldown_period:
                    msg = f"*{label}* on host *{host_ip}* is at *{value:.1f}%* (Threshold: {limit}%)"
                    await cls.dispatch_webhook(msg)
                    cls._cooldowns[key] = now

# --- VM Operations ---

def wait_for_task(task, timeout=30):
    """Wait for a vSphere task to finish or timeout."""
    start_time = datetime.now()
    while task.info.state in [vim.TaskInfo.State.running, vim.TaskInfo.State.queued]:
        time.sleep(0.5)
        if (datetime.now() - start_time).total_seconds() > timeout:
            return False, "Task timed out."
    
    if task.info.state == vim.TaskInfo.State.success:
        return True, "Success"
    else:
        return False, str(task.info.error.msg) if task.info.error else "Unknown error"

def vm_power_action(host_ip, vm_name, action):
    """
    Performs power operations on a VM and triggers an immediate refresh.
    """
    with database.SessionLocal() as db:
        host_row = db.query(ESXiHost).filter_by(ip=host_ip).first()
        if not host_row:
            return False, "Host not found in database."
        user, password = host_row.username, host_row.password

    si = get_si(host_ip, user, password)
    if not si:
        return False, f"Failed to connect to host {host_ip}."

    try:
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        vm = next((v for v in container.view if v.name == vm_name), None)
        container.Destroy()

        if not vm:
            return False, f"VM '{vm_name}' not found."

        # Trigger Power Action
        if action == 'on': task = vm.PowerOn()
        elif action == 'off': task = vm.PowerOff()
        elif action == 'reset': task = vm.ResetVM_Task()
        elif action == 'suspend': task = vm.SuspendVM_Task()
        else: 
            return False, "Invalid action"

        # Wait for the task to complete for immediate feedback
        success, msg = wait_for_task(task)
        
        if not success:
            return False, f"Task failed: {msg}"

        return True, f"Action {action} completed for {vm_name}."
    except Exception as e:
        return False, str(e)

# --- Helper Functions ---

def format_guest_id(guest_id):
    if not guest_id: return "Unknown"
    guest_id = guest_id.replace("Guest", "").replace("_", " ")
    return guest_id.capitalize()

def connect_host(host, user, password):
    # Create a more robust SSL context that handles legacy ESXi TLS versions/ciphers
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        # Support older ESXi versions by allowing legacy ciphers if needed
        context.set_ciphers('DEFAULT@SECLEVEL=1') 
    except Exception:
        context = ssl._create_unverified_context() if hasattr(ssl, '_create_unverified_context') else None

    try:
        # Use a reasonable timeout for connection
        si = connect.SmartConnect(
            host=host, user=user, pwd=password, 
            sslContext=context, disableSslCertValidation=True,
            connectionPoolTimeout=30 # Increased timeout
        )
        return si
    except Exception as e:
        # Suppress noisy SSL logs if they are expected but still print connection failures
        err_msg = str(e)
        if "unknown error" in err_msg or "Remote end closed" in err_msg:
            print(f"[{datetime.now()}] Connection failure for {host} (likely SSL/TLS mismatch or rate-limit): {err_msg}")
        else:
            print(f"Failed to connect to {host}: {e}")
        return None

# --- Connection Pooling & Sessions ---
_session_cache = {} # {(host, user): (si, expiry)}
_session_lock = threading.Lock()

def get_si(host, user, password):
    """Returns a cached or fresh dedicated connection for thread safety and performance."""
    key = (host, user)
    with _session_lock:
        if key in _session_cache:
            si, expiry = _session_cache[key]
            if datetime.now() < expiry:
                try:
                    si.CurrentTime() # Test connection
                    return si
                except Exception:
                    pass # Connection dead
            del _session_cache[key]

    si = connect_host(host, user, password)
    if si:
        with _session_lock:
            # Cache for 10 minutes to survive multiple fast-tier syncs
            _session_cache[key] = (si, datetime.now() + timedelta(minutes=10))
    return si

def bulk_fetch_infrastructure(si, host_id_to_fetch=None):
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem, vim.VirtualMachine], True)

    try:
        traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(name='t', path='view', skip=False, type=vim.view.ContainerView)
        host_spec = vmodl.query.PropertyCollector.PropertySpec(type=vim.HostSystem, pathSet=['name', 'summary.hardware', 'summary.quickStats', 'datastore'])
        # Optimized VM property set for "best performance ever"
        vm_spec = vmodl.query.PropertyCollector.PropertySpec(type=vim.VirtualMachine, pathSet=[
            'name', 
            'runtime.powerState', 
            'summary.quickStats', 
            'summary.config', 
            'guest.ipAddress', 
            'summary.guest.guestFullName',
            'summary.storage',
            'config.hardware.device', # Still needed for accurate disk KB, but we can potentially optimize
            'config.createDate'
        ])

        filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=[vmodl.query.PropertyCollector.ObjectSpec(obj=container, selectSet=[traversal_spec])], propSet=[host_spec, vm_spec])
        props = content.propertyCollector.RetrievePropertiesEx([filter_spec], vmodl.query.PropertyCollector.RetrieveOptions())

        parsed_hosts, parsed_vms = [], []
        for obj_content in (props.objects if props else []):
            properties = {prop.name: prop.val for prop in obj_content.propSet}
            if isinstance(obj_content.obj, vim.HostSystem):
                hw, stats = properties.get('summary.hardware'), properties.get('summary.quickStats')
                
                cpu_mhz = (hw.cpuMhz * hw.numCpuThreads) if (hw and hw.cpuMhz and hw.numCpuThreads) else 0
                cpu_used = stats.overallCpuUsage if (stats and stats.overallCpuUsage) else 0
                mem_total = (hw.memorySize / (1024**2)) if (hw and hw.memorySize) else 0
                mem_used = stats.overallMemoryUsage if (stats and stats.overallMemoryUsage) else 0
                
                total_s = sum(ds.summary.capacity for ds in properties.get('datastore', []) if ds.summary)
                free_s = sum(ds.summary.freeSpace for ds in properties.get('datastore', []) if ds.summary)
                
                parsed_hosts.append({'name': properties.get('name'), 'cpu_mhz': cpu_mhz, 'cpu_used': cpu_used, 'mem_total': mem_total, 'mem_used': mem_used, 'storage_total': total_s, 'storage_free': free_s})
            elif isinstance(obj_content.obj, vim.VirtualMachine):
                stats = properties.get('summary.quickStats')
                config = properties.get('summary.config')
                power = properties.get('runtime.powerState')
                ip = properties.get('guest.ipAddress')
                os_name = properties.get('summary.guest.guestFullName')
                devices = properties.get('config.hardware.device', [])
                
                disk_kb = sum(dev.capacityInKB for dev in devices if isinstance(dev, vim.vm.device.VirtualDisk))
                
                vm_cpu_used = stats.overallCpuUsage if (stats and stats.overallCpuUsage) else 0
                vm_mem_used = stats.guestMemoryUsage if (stats and stats.guestMemoryUsage) else 0
                vm_mem_total = config.memorySizeMB if (config and config.memorySizeMB) else 0
                vm_cpus = config.numCpu if (config and config.numCpu) else 0
                
                parsed_vms.append({
                    'name': properties.get('name'), 
                    'power': str(power), 
                    'cpu_used': vm_cpu_used, 
                    'mem_used': vm_mem_used, 
                    'mem_total': vm_mem_total, 
                    'num_cpu': vm_cpus, 
                    'ip': ip if ip else "N/A", 
                    'os': os_name if os_name else "Unknown", 
                    'create_date': properties.get('config.createDate'), 
                    'disk_gb': round(disk_kb / (1024**2), 2)
                })
        return {'hosts': parsed_hosts, 'vms': parsed_vms}
    finally:
        container.Destroy()

def collect_host_data(host_id):
    """Collects and UPSERTS data to ensure persistence and real-time updates."""
    with database.SessionLocal() as db:
        host_row = db.query(ESXiHost).get(host_id)
        if not host_row: return
        ip, user, password = host_row.ip, host_row.username, host_row.password

    print(f"[{datetime.now()}] Thread starting collection for {ip}")
    si = get_si(ip, user, password)
    if not si: 
        print(f"[{datetime.now()}] Thread failed to connect to {ip}")
        return

    try:
        infra = bulk_fetch_infrastructure(si, host_id_to_fetch=host_id)
        if not infra['hosts']: 
            print(f"[{datetime.now()}] Thread found no host data for {ip}")
            return
        h = infra['hosts'][0]
        
        # 1. Update Host Sync Timestamp
        with database.SessionLocal() as db:
            host_row = db.query(ESXiHost).get(host_id)
            if host_row:
                host_row.last_synced = datetime.now()
            
            # 2. Add Host Metrics (Persistence)
            metrics = HostMetrics(
                host_id=host_id,
                cpu_usage=round((h['cpu_used'] / h['cpu_mhz']) * 100, 2) if h['cpu_mhz'] > 0 else 0,
                used_cpu_ghz=round(h['cpu_used'] / 1000, 2),
                total_cpu_ghz=round(h['cpu_mhz'] / 1000, 2),
                mem_usage=round((h['mem_used'] / h['mem_total']) * 100, 2) if h['mem_total'] > 0 else 0,
                used_mem_gb=round(h['mem_used'] / 1024, 2),
                total_mem_gb=round(h['mem_total'] / 1024, 2),
                storage_usage=round(((h['storage_total'] - h['storage_free']) / h['storage_total']) * 100, 2) if h['storage_total'] > 0 else 0,
                used_storage_gb=round((h['storage_total'] - h['storage_free']) / (1024**3), 2),
                total_storage_gb=round(h['storage_total'] / (1024**3), 2),
                last_updated=datetime.now()
            )
            db.add(metrics)

            # 3. Upsert VMs (No more bulk delete!)
            existing_vms = {vm.name: vm for vm in db.query(VM).filter_by(host_id=host_id).all()}
            for vm_data in infra['vms']:
                vm = existing_vms.get(vm_data['name'])
                if not vm:
                    vm = VM(name=vm_data['name'], host_id=host_id)
                    db.add(vm)
                
                vm.os = vm_data['os']
                
                # Intelligent IP persistence
                new_ip = vm_data['ip']
                if new_ip and new_ip != "N/A":
                    vm.ip = new_ip
                elif vm_data['power'] == 'poweredOff':
                    # If powered off and we don't have an IP from vSphere, we can keep the old one 
                    # but maybe we should tag it as (Last Known) or similar in UI? 
                    # For now, just keep it to avoid "Unknown"
                    pass 
                
                vm.cpu_count = vm_data['num_cpu']
                vm.cpu_usage_mhz = vm_data['cpu_used']
                vm.ram_used_mb = vm_data['mem_used']
                vm.ram_total_mb = vm_data['mem_total']
                vm.ram_usage = round((vm_data['mem_used'] / vm_data['mem_total']) * 100, 1) if vm_data['mem_total'] > 0 else 0
                vm.ram_info = f"{vm_data['mem_used']} / {vm_data['mem_total']} MB"
                vm.disk_total_gb = vm_data['disk_gb']
                vm.power_state = vm_data['power']
                vm.last_updated = datetime.now()

            # 4. Remove VMs no longer on host
            current_vm_names = {v_data['name'] for v_data in infra['vms']}
            for name, vm in existing_vms.items():
                if name not in current_vm_names:
                    db.delete(vm)

            db.commit()
            print(f"[{datetime.now()}] Thread completed collection for {ip}")
            
            # Async Alert Check
            asyncio.run(AlertDispatcher.check_thresholds(ip, metrics.cpu_usage, metrics.mem_usage, metrics.storage_usage))
            
    except Exception as e:
        print(f"Collection error for {ip}: {e}")

# --- Subnet & Collection Loops ---

_scan_status = {"running": False}

def is_scan_running():
    return _scan_status["running"]

def scan_all_subnets():
    _scan_status["running"] = True
    try:
        asyncio.run(async_scan_all_subnets())
    finally:
        _scan_status["running"] = False

def scan_single_subnet(prefix):
    """Synchronously scan a single subnet."""
    asyncio.run(async_scan_and_store_subnet(prefix))

def collect_specific_vm_data(host_id, vm_name):
    """Targeted update for a single VM to provide 'best performance ever'."""
    with database.SessionLocal() as db:
        host_row = db.query(ESXiHost).get(host_id)
        if not host_row: return
        ip, user, password = host_row.ip, host_row.username, host_row.password

    si = get_si(ip, user, password)
    if not si: return

    try:
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        vm_obj = next((v for v in container.view if v.name == vm_name), None)
        
        if vm_obj:
            # If VM is powered on, we might need to wait a few seconds for VMware tools to report IP
            # We'll try up to 3 times with 2s delay if state is On but IP is missing
            for attempt in range(3):
                prop_spec = vmodl.query.PropertyCollector.PropertySpec(
                    type=vim.VirtualMachine, 
                    pathSet=['runtime.powerState', 'summary.quickStats', 'summary.config', 'guest.ipAddress']
                )
                object_spec = vmodl.query.PropertyCollector.ObjectSpec(obj=vm_obj)
                filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=[object_spec], propSet=[prop_spec])
                
                props = content.propertyCollector.RetrievePropertiesEx([filter_spec], vmodl.query.PropertyCollector.RetrieveOptions())
                
                vm_props = {}
                if props and props.objects:
                    for prop in props.objects[0].propSet:
                        vm_props[prop.name] = prop.val

                raw_power = vm_props.get('runtime.powerState')
                ip_addr = vm_props.get('guest.ipAddress')

                if str(raw_power) == "poweredOn" and not ip_addr and attempt < 2:
                    print(f"[{datetime.now()}] VM {vm_name} is On but IP missing. Retrying in 2s... (Attempt {attempt+1})")
                    time.sleep(2.0)
                    continue
                break

            with database.SessionLocal() as db:
                vm = db.query(VM).filter_by(host_id=host_id, name=vm_name).first()
                if vm:
                    print(f"[{datetime.now()}] VM {vm_name} power state from ESXi: {raw_power}")
                    vm.power_state = str(raw_power)
                    
                    if ip_addr: 
                        vm.ip = ip_addr
                    # Note: We specifically DO NOT overwrite vm.ip with None/N/A here 
                    # to avoid losing the last known IP while booting or if tools stop.
                    
                    stats = vm_props.get('summary.quickStats')
                    config = vm_props.get('summary.config')
                    if stats:
                        vm.cpu_usage_mhz = stats.overallCpuUsage
                        vm.ram_used_mb = stats.guestMemoryUsage
                    if config:
                        vm.ram_total_mb = config.memorySizeMB
                        vm.ram_usage = round((stats.guestMemoryUsage / config.memorySizeMB) * 100, 1) if config.memorySizeMB > 0 else 0
                        vm.ram_info = f"{stats.guestMemoryUsage} / {config.memorySizeMB} MB"
                    
                    vm.last_updated = datetime.now()
                    db.commit()
                    print(f"[{datetime.now()}] VM {vm_name} updated in DB with state {vm.power_state} and IP {vm.ip}")
        container.Destroy()
    except Exception as e:
        print(f"Targeted VM collection error: {e}")

def update_all_hosts():
    with database.SessionLocal() as db:
        host_ids = [h.id for h in db.query(ESXiHost).all()]
    with ThreadPoolExecutor(max_workers=32) as executor:
        executor.map(collect_host_data, host_ids)
    # Pruning
    with database.SessionLocal() as db:
        cutoff = datetime.now() - timedelta(days=7)
        db.query(HostMetrics).filter(HostMetrics.last_updated < cutoff).delete()
        db.commit()

def update_single_host_by_ip(host_ip, vm_name=None):
    with database.SessionLocal() as db:
        host = db.query(ESXiHost).filter_by(ip=host_ip).first()
        if host:
            if vm_name:
                collect_specific_vm_data(host.id, vm_name)
            else:
                collect_host_data(host.id)

async def async_scan_and_store_subnet(prefix):
    ips = [f"{prefix}.{i}" for i in range(256)]
    # Limit concurrency to avoid resource exhaustion
    sem = asyncio.Semaphore(50)
    tasks = [async_scan_ip(ip, sem) for ip in ips]
    results = await asyncio.gather(*tasks)
    
    with database.SessionLocal() as db:
        # Fetch current state in one go
        existing_leases = {l.ip: l for l in db.query(IPLease).filter_by(subnet=prefix).all()}
        
        for ip, active, prot in results:
            lease = existing_leases.get(ip)
            new_status = IPStatus.ACTIVE if active else (IPStatus.RESERVED if (lease and lease.status in [IPStatus.ACTIVE, IPStatus.RESERVED]) else IPStatus.FREE)
            
            if not lease:
                lease = IPLease(ip=ip, subnet=prefix, status=new_status, last_updated=datetime.now())
                db.add(lease)
                # Only log if it's NOT free (avoid bloating logs with 254 'Free' entries)
                if new_status != IPStatus.FREE:
                    db.add(HistoryLog(ip=ip, status=new_status, timestamp=datetime.now()))
            elif lease.status != new_status:
                lease.status = new_status
                lease.last_updated = datetime.now()
                db.add(HistoryLog(ip=ip, status=new_status, timestamp=datetime.now()))
        
        db.commit()

async def async_scan_all_subnets():
    subnets = database.get_all_subnets()
    tasks = [async_scan_and_store_subnet(s) for s in subnets]
    await asyncio.gather(*tasks)

async def async_scan_ip(ip, sem):
    async with sem:
        # Faster Ping
        cmd = ['ping', '-c', '1', '-W', '0.5', ip]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await proc.wait()
        if proc.returncode == 0: return ip, True, "ICMP"
        return ip, False, None
