import ssl
import re
import ipaddress
import shutil
import platform
import subprocess
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import requests
import asyncio
import aiohttp
import os
from pyVim import connect
from pyVmomi import vim, vmodl

# Suppress verbose pyVmomi/suds internal logs (e.g. "Finding item by path")
logging.getLogger("pyVmomi").setLevel(logging.WARNING)
logging.getLogger("suds").setLevel(logging.WARNING)
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import database
from models import ESXiHost, HostMetrics, VM, VMDevice, NetworkDevice, IPLease, HistoryLog, IPStatus, VMSnapshot

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

_IPV4_CANDIDATE_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

def _normalize_machine_ip(candidate):
    """Return a normalized IPv4 string only when it is in 192.168.x.x."""
    if not candidate:
        return None
    try:
        normalized = str(ipaddress.IPv4Address(str(candidate).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return None
    return normalized if normalized.startswith("192.168.") else None

def _extract_machine_ip(raw_value):
    """
    Extract the first valid VM LAN IP from vSphere fields.
    Keeps only 192.168.x.x IPv4 values and ignores service/cluster IPs.
    """
    if raw_value is None:
        return None

    if isinstance(raw_value, str):
        candidates = _IPV4_CANDIDATE_RE.findall(raw_value)
        if not candidates and raw_value.strip():
            candidates = [raw_value.strip()]
    elif isinstance(raw_value, (list, tuple, set)):
        candidates = []
        for item in raw_value:
            if item is None:
                continue
            candidates.extend(_IPV4_CANDIDATE_RE.findall(str(item)))
    else:
        candidates = _IPV4_CANDIDATE_RE.findall(str(raw_value))

    for candidate in candidates:
        normalized = _normalize_machine_ip(candidate)
        if normalized:
            return normalized
    return None

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
            # Cache for 45 minutes to survive the 30-minute sync intervals
            _session_cache[key] = (si, datetime.now() + timedelta(minutes=45))
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

                cpu_mhz = (hw.cpuMhz * hw.numCpuCores) if (hw and hw.cpuMhz and hw.numCpuCores) else 0
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
                ip = _extract_machine_ip(properties.get('guest.ipAddress'))
                os_name = properties.get('summary.guest.guestFullName')
                devices = properties.get('config.hardware.device', [])

                disk_kb = sum(dev.capacityInKB for dev in devices if isinstance(dev, vim.vm.device.VirtualDisk))

                passthrough_devices = []
                for dev in devices:
                    di = getattr(dev, 'deviceInfo', None)
                    label = (di.label if di else '') or ''
                    summary = (di.summary if di else '') or ''
                    if isinstance(dev, vim.vm.device.VirtualUSB):
                        connected = bool(getattr(dev, 'connected', False))
                        passthrough_devices.append({'type': 'USB', 'label': label, 'summary': summary, 'connected': connected})
                    elif isinstance(dev, vim.vm.device.VirtualPCIPassthrough):
                        passthrough_devices.append({'type': 'PCI Passthrough', 'label': label, 'summary': summary, 'connected': True})
                    elif isinstance(dev, vim.vm.device.VirtualSCSIPassthrough):
                        passthrough_devices.append({'type': 'SCSI Passthrough', 'label': label, 'summary': summary, 'connected': True})

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
                    'disk_gb': round(disk_kb / (1024**2), 2),
                    'devices': passthrough_devices,
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
                    existing_vms[vm_data['name']] = vm  # track new VMs for device sync

                vm.os = vm_data['os']

                # Intelligent IP persistence
                new_ip = _extract_machine_ip(vm_data['ip'])
                if new_ip:
                    vm.ip = new_ip
                elif vm.ip and not _extract_machine_ip(vm.ip):
                    vm.ip = None

                vm.cpu_count = vm_data['num_cpu']
                vm.cpu_usage_mhz = vm_data['cpu_used']
                vm.ram_used_mb = vm_data['mem_used']
                vm.ram_total_mb = vm_data['mem_total']
                vm.ram_usage = round((vm_data['mem_used'] / vm_data['mem_total']) * 100, 1) if vm_data['mem_total'] > 0 else 0
                vm.ram_info = f"{vm_data['mem_used']} / {vm_data['mem_total']} MB"
                vm.disk_total_gb = vm_data['disk_gb']
                vm.power_state = vm_data['power']
                vm.last_updated = datetime.now()
                # Persist creation date (only set once; never overwrite with None)
                if vm_data['create_date'] and not vm.created_date:
                    raw_cd = vm_data['create_date']
                    if hasattr(raw_cd, 'replace'):
                        vm.created_date = raw_cd.replace(tzinfo=None).isoformat()
                    else:
                        vm.created_date = str(raw_cd)[:19]

            # 4. Remove VMs no longer on host
            current_vm_names = {v_data['name'] for v_data in infra['vms']}
            for name, vm in existing_vms.items():
                if name not in current_vm_names:
                    db.delete(vm)

            # 5. Sync passthrough/USB devices — flush first so new VMs have IDs
            db.flush()
            for vm_data in infra['vms']:
                vm = existing_vms.get(vm_data['name'])
                if not vm or not vm.id:
                    continue
                db.query(VMDevice).filter_by(vm_id=vm.id).delete()
                for dev in vm_data.get('devices', []):
                    db.add(VMDevice(
                        vm_id=vm.id,
                        host_id=host_id,
                        device_type=dev['type'],
                        device_label=dev['label'],
                        device_summary=dev['summary'],
                        connected=dev['connected'],
                        last_updated=datetime.now()
                    ))

            # 6. Write VMSnapshot records for all VMs (for History / DR)
            snap_ts = datetime.now()
            for vm_data in infra['vms']:
                snapshot_ip = _extract_machine_ip(vm_data['ip'])
                db.add(VMSnapshot(
                    timestamp=snap_ts,
                    vm_name=vm_data['name'],
                    host_id=host_id,
                    host_ip=ip,
                    power_state=vm_data['power'],
                    ip_address=snapshot_ip,
                    os=vm_data['os'],
                ))

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
                ip_addr = _extract_machine_ip(vm_props.get('guest.ipAddress'))

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
                    elif vm.ip and not _extract_machine_ip(vm.ip):
                        vm.ip = None
                    # Keep last known LAN IP when ESXi temporarily has no usable 192.168.x.x IP.
                    # We only clear values that are non-LAN/service IPs.

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

class VMEventWatcher:
    """
    Long-running per-host thread that detects VM power state changes in real time
    using vSphere's WaitForUpdatesEx API (push-like, no polling).

    On each detected change it:
      1. Updates the VM row in the DB immediately.
      2. Writes a VMSnapshot (used by History/DR).
      3. Sets 'last_event_ts' in app_settings so the dashboard knows to refresh.
    """

    def __init__(self, host_id: int):
        self.host_id = host_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"vm-watcher-{self.host_id}"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ internals

    def _run(self):
        """Outer retry loop — reconnects if the inner watch loop crashes."""
        while not self._stop.is_set():
            try:
                self._watch_loop()
            except Exception as e:
                print(f"[EventWatcher host={self.host_id}] Unhandled crash: {e}. Retrying in 30 s.")
            self._stop.wait(30)

    def _watch_loop(self):
        """Inner loop: establish a fresh connection and run WaitForUpdatesEx."""
        with database.SessionLocal() as db:
            host_row = db.query(ESXiHost).get(self.host_id)
            if not host_row:
                return
            ip, user, password = host_row.ip, host_row.username, host_row.password

        print(f"[EventWatcher] {ip}: connecting …")
        # Use a dedicated connection so it doesn't interfere with the 30-min collector
        si = connect_host(ip, user, password)
        if not si:
            print(f"[EventWatcher] {ip}: connection failed, will retry in 60 s.")
            self._stop.wait(60)
            return

        content = si.RetrieveContent()
        pc = content.propertyCollector
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )

        try:
            traversal = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseContents", path="view", skip=False,
                type=vim.view.ContainerView
            )
            prop_spec = vmodl.query.PropertyCollector.PropertySpec(
                type=vim.VirtualMachine,
                pathSet=["name", "runtime.powerState", "guest.ipAddress"]
            )
            obj_spec = vmodl.query.PropertyCollector.ObjectSpec(
                obj=container, selectSet=[traversal]
            )
            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[obj_spec], propSet=[prop_spec]
            )
            pc_filter = pc.CreateFilter(filter_spec, True)

            # WaitForUpdatesEx blocks server-side up to maxWaitSeconds, then
            # returns None on timeout or a result set when properties change.
            wait_opts = vmodl.query.PropertyCollector.WaitOptions(maxWaitSeconds=30)

            # --- Initial population (version="") ---
            # First call returns all current property values so we know each
            # VM's MOR → name mapping. We don't treat this as "new events".
            try:
                result = pc.WaitForUpdatesEx("", wait_opts)
                version = result.version if result else ""
            except Exception as e:
                print(f"[EventWatcher] {ip}: initial WaitForUpdatesEx failed: {e}")
                return

            # Build MOR → name map from the initial populate
            vm_name_map: dict[str, str] = {}  # str(MOR) → vm_name
            if result:
                for fs in result.filterSet:
                    for obj_set in fs.objectSet:
                        mor_key = str(obj_set.obj)
                        for ch in obj_set.changeSet:
                            if ch.name == "name":
                                vm_name_map[mor_key] = ch.val

            print(f"[EventWatcher] {ip}: watching {len(vm_name_map)} VMs.")

            # --- Incremental event loop ---
            while not self._stop.is_set():
                try:
                    result = pc.WaitForUpdatesEx(version, wait_opts)
                    if result is None:
                        continue  # server-side timeout, no changes — keep waiting

                    version = result.version
                    events = self._parse_result(result, vm_name_map)
                    if events:
                        self._persist_events(events, ip)

                except vmodl.fault.ManagedObjectNotFound:
                    print(f"[EventWatcher] {ip}: managed object gone, resetting.")
                    break
                except Exception as e:
                    msg = str(e).lower()
                    if any(x in msg for x in ("not authenticated", "session", "connect")):
                        print(f"[EventWatcher] {ip}: session expired, reconnecting.")
                        break
                    print(f"[EventWatcher] {ip}: error: {e}")
                    self._stop.wait(5)

        finally:
            for cleanup in (pc_filter.Destroy, container.Destroy, lambda: connect.Disconnect(si)):
                try:
                    cleanup()
                except Exception:
                    pass

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _parse_result(result, vm_name_map: dict) -> list[tuple]:
        """
        Extract (vm_name, power_state, ip_addr) tuples from a WaitForUpdatesEx result.
        Only 'modify' events carry real changes; 'enter' updates the name map.
        """
        events = []
        for fs in result.filterSet:
            for obj_set in fs.objectSet:
                mor_key = str(obj_set.obj)
                power_state = None
                ip_addr = None
                vm_name = None

                for ch in obj_set.changeSet:
                    if ch.name == "name":
                        vm_name_map[mor_key] = ch.val  # keep map up-to-date
                        vm_name = ch.val
                    elif ch.name == "runtime.powerState":
                        power_state = str(ch.val)
                    elif ch.name == "guest.ipAddress":
                        ip_addr = _extract_machine_ip(ch.val)

                # obj_set.kind == 'leave' means VM was deleted — skip
                if obj_set.kind == "leave":
                    vm_name_map.pop(mor_key, None)
                    continue

                vm_name = vm_name or vm_name_map.get(mor_key)
                if power_state and vm_name:
                    events.append((vm_name, power_state, ip_addr))

        return events

    def _persist_events(self, events: list[tuple], host_ip: str):
        """Write detected changes to DB instantly and signal the dashboard."""
        now = datetime.now()
        changed = False

        with database.SessionLocal() as db:
            for vm_name, power_state, ip_addr in events:
                vm = db.query(VM).filter_by(host_id=self.host_id, name=vm_name).first()
                if not vm:
                    continue  # VM not yet in DB (pre-first-collection); skip
                old_state = vm.power_state
                vm.power_state = power_state
                if ip_addr:
                    vm.ip = ip_addr
                elif vm.ip and not _extract_machine_ip(vm.ip):
                    vm.ip = None
                snapshot_ip = ip_addr or _extract_machine_ip(vm.ip)
                vm.last_updated = now
                db.add(VMSnapshot(
                    timestamp=now, vm_name=vm_name,
                    host_id=self.host_id, host_ip=host_ip,
                    power_state=power_state,
                    ip_address=snapshot_ip,
                    os=vm.os,
                ))
                changed = True
                print(f"[EventWatcher] {host_ip}: {vm_name}  {old_state} → {power_state}")
            if changed:
                db.commit()

        if changed:
            # Signal the Streamlit dashboard fragments to refresh
            database.set_setting("last_event_ts", now.isoformat())


def update_all_hosts():
    with database.SessionLocal() as db:
        host_ids = [h.id for h in db.query(ESXiHost).all()]
    with ThreadPoolExecutor(max_workers=max(len(host_ids), 1)) as executor:
        list(executor.map(collect_host_data, host_ids))
    # Pruning old metrics and snapshots (keep 30 days)
    with database.SessionLocal() as db:
        cutoff = datetime.now() - timedelta(days=30)
        db.query(HostMetrics).filter(HostMetrics.last_updated < cutoff).delete()
        db.query(VMSnapshot).filter(VMSnapshot.timestamp < cutoff).delete()
        db.commit()
    # Signal dashboard that fresh data is available
    database.set_setting("last_collection_ts", datetime.now().isoformat())

def update_single_host_by_ip(host_ip, vm_name=None):
    with database.SessionLocal() as db:
        host = db.query(ESXiHost).filter_by(ip=host_ip).first()
        if host:
            if vm_name:
                collect_specific_vm_data(host.id, vm_name)
            else:
                collect_host_data(host.id)

async def _dns_reverse_lookup(ip):
    """Run nslookup and return (hostname, True) if the IP resolves, else (None, False)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            'nslookup', ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        output = stdout.decode('utf-8', errors='ignore')
        for line in output.splitlines():
            # nslookup reverse lookup lines: "192.168.x.x.in-addr.arpa  name = hostname."
            if 'name =' in line.lower():
                hostname = line.split('=')[-1].strip().rstrip('.')
                if hostname:
                    return hostname
    except Exception:
        pass
    return None

async def _nmap_scan(ip):
    """Run nmap port scan; return True if any common port is open."""
    if not shutil.which('nmap'):
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            'nmap', '-p', '22,80,443,445,3389', '--open', '-T4', '-n', '--host-timeout', '3s', ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return b'open' in stdout
    except Exception:
        return False

async def async_scan_ip(ip, sem):
    """
    Scan a single IP with three fallback tiers:
    1. ICMP ping      → ACTIVE
    2. nslookup (DNS) → RESERVED (machine exists in DNS but may be offline)
    3. nmap port scan → ACTIVE  (ports open but ping blocked)
    Returns: (ip, active: bool, protocol: str | None, hostname: str | None)
    """
    async with sem:
        # Tier 1: ICMP ping
        cmd = ['ping', '-c', '1', '-W', '0.5', ip]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await proc.wait()
        if proc.returncode == 0:
            return ip, True, "ICMP", None

        # Tier 2: DNS reverse lookup — reserved even if offline
        hostname = await _dns_reverse_lookup(ip)
        if hostname:
            return ip, False, "DNS", hostname

        # Tier 3: nmap port scan — catches hosts blocking ICMP
        if await _nmap_scan(ip):
            return ip, True, "NMAP", None

        return ip, False, None, None

async def async_scan_and_store_subnet(prefix):
    now = datetime.now()
    ips_to_scan = []

    # 1. Passive Discovery: Get all IPs known to be assigned to VMs on this subnet
    with database.SessionLocal() as db:
        vm_ips = {v.ip for v in db.query(VM).filter(VM.ip.like(f"{prefix}.%")).all() if v.ip}
        existing_leases = {l.ip: l for l in db.query(IPLease).filter_by(subnet=prefix).all()}

    # 2. Adaptive Logic: Decide which IPs actually need an active probe
    for i in range(256):
        ip = f"{prefix}.{i}"

        # If it's a known VM IP, we already know it's ACTIVE (Passive Discovery)
        if ip in vm_ips:
            continue

        lease = existing_leases.get(ip)
        if lease:
            # If it was FREE and checked recently (last 2 hours), skip it to save bandwidth
            if lease.status == IPStatus.FREE and (now - lease.last_updated) < timedelta(hours=2):
                continue
            # If it was DOWN/RESERVED, we check it every cycle but maybe with lower priority?
            # For now, just include it.

        ips_to_scan.append(ip)

    print(f"[{now}] Scanning {len(ips_to_scan)} IPs on subnet {prefix} (Adaptive: skipped {256 - len(ips_to_scan) - len(vm_ips)} IPs)")

    sem = asyncio.Semaphore(50)
    tasks = [async_scan_ip(ip, sem) for ip in ips_to_scan]
    results = await asyncio.gather(*tasks)

    # Combine results with Passive Discovery data
    final_results = []
    for ip in vm_ips:
        final_results.append((ip, True, "VM_GUEST", None))
    final_results.extend(results)

    with database.SessionLocal() as db:
        # Re-fetch existing leases within the write session
        existing_leases = {l.ip: l for l in db.query(IPLease).filter_by(subnet=prefix).all()}

        for ip, active, prot, dns_hostname in final_results:
            lease = existing_leases.get(ip)

            if active:
                new_status = IPStatus.ACTIVE
            elif dns_hostname:
                new_status = IPStatus.RESERVED
            elif lease and lease.status in [IPStatus.ACTIVE, IPStatus.RESERVED]:
                new_status = IPStatus.RESERVED
            else:
                new_status = IPStatus.FREE

            device_id = lease.device_id if lease else None
            if dns_hostname:
                dev = db.query(NetworkDevice).filter_by(hostname=dns_hostname).first()
                if not dev:
                    dev = NetworkDevice(hostname=dns_hostname, first_seen=now, last_seen=now)
                    db.add(dev)
                    db.flush()
                else:
                    dev.last_seen = now
                device_id = dev.id

            if not lease:
                lease = IPLease(
                    ip=ip, subnet=prefix, status=new_status,
                    dns_hostname=dns_hostname, device_id=device_id, last_updated=now
                )
                db.add(lease)
                if new_status != IPStatus.FREE:
                    db.add(HistoryLog(
                        ip=ip, status=new_status, timestamp=now,
                        hostname_snapshot=dns_hostname, device_id=device_id
                    ))
            else:
                changed = lease.status != new_status
                hostname_changed = dns_hostname and lease.dns_hostname != dns_hostname
                if changed or hostname_changed:
                    lease.status = new_status
                    lease.last_updated = now
                    if dns_hostname:
                        lease.dns_hostname = dns_hostname
                    if device_id:
                        lease.device_id = device_id
                    if changed:
                        db.add(HistoryLog(
                            ip=ip, status=new_status, timestamp=now,
                            hostname_snapshot=dns_hostname or lease.dns_hostname,
                            device_id=lease.device_id
                        ))
                else:
                    # Even if nothing changed, update last_updated so Adaptive Scan knows we checked it
                    lease.last_updated = now

        db.commit()

async def async_scan_all_subnets():
    subnets = database.get_all_subnets()
    tasks = [async_scan_and_store_subnet(s) for s in subnets]
    await asyncio.gather(*tasks)
