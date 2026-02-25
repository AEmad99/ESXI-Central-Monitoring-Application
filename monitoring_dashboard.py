import re
import streamlit as st
import requests
import streamlit_authenticator as stauth
import json
import platform
from concurrent.futures import ThreadPoolExecutor
import time
import os
from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import joinedload

# --- New Modules ---
import database
from models import ESXiHost, VM, IPLease, HistoryLog, NetworkDevice, Subnet, IPStatus
import data_collector
from dotenv import load_dotenv
import ai_agent
import background_job

# Load environment variables
load_dotenv()

# Disable SSL warnings for self-signed certificates
requests.packages.urllib3.disable_warnings()

st.set_page_config(layout="wide", page_title="ESXi Monitoring Dashboard", initial_sidebar_state="collapsed")

# --- Database Initialization & Seeding ---
# Host Groups (Loaded from .env JSON)
try:
    host_groups_json = os.getenv("HOST_GROUPS_JSON", "{}")
    if host_groups_json.startswith("'") and host_groups_json.endswith("'"):
        host_groups_json = host_groups_json[1:-1]
        
    raw_groups = json.loads(host_groups_json)
    HOST_GROUPS = {}
    for group_name, data in raw_groups.items():
        pass_env_var = data.get("pass_env")
        password = os.getenv(pass_env_var) if pass_env_var else None
        
        HOST_GROUPS[group_name] = {
            "ips": data.get("ips", []),
            "pass": password,
            "user": data.get("user", "root")
        }
except json.JSONDecodeError as e:
    st.error(f"Failed to parse HOST_GROUPS_JSON from .env: {e}")
    HOST_GROUPS = {}
except Exception as e:
    st.error(f"Error loading host configuration: {e}")
    HOST_GROUPS = {}

# Ensure DB is ready (only on first run, not every Streamlit rerun)
if not st.session_state.get('db_initialized'):
    database.init_db()
    database.update_hosts_from_config(HOST_GROUPS)
    database.seed_subnets_if_empty()
    st.session_state.db_initialized = True

# Start background collector thread (once per process, not every rerun)
if not st.session_state.get('_bg_started'):
    background_job.start()
    st.session_state._bg_started = True

# --- Theme Management ---
if "theme" in st.query_params:
    param_theme = st.query_params["theme"]
    if param_theme in ["Light", "Dark"]:
        st.session_state.theme = param_theme

if 'theme' not in st.session_state:
    st.session_state.theme = 'Light'

@st.cache_data(show_spinner=False)
def get_theme_css(mode):
    # Common Styles
    common_css = """
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&display=swap');
    html, body, [class*="css"] { font-family: 'Outfit', sans-serif !important; }
    h1, h2, h3 { font-weight: 500 !important; letter-spacing: -0.02em !important; }
    .stButton > button { border-radius: 8px !important; font-weight: 500 !important; transition: all 0.2s cubic-bezier(0.2, 0, 0, 1) !important; }
    .stButton > button:hover { transform: translateY(-2px); }
    .link-button { text-decoration: none !important; padding: 0.6rem 1.2rem; border-radius: 8px !important; text-align: center; cursor: pointer; display: block; width: 100%; box-sizing: border-box; font-size: 0.9rem; font-weight: 500; transition: all 0.2s ease; box-shadow: 0 2px 6px rgba(0,0,0,0.1); }
    .link-button:hover { transform: translateY(-2px); box-shadow: 0 6px 12px rgba(0,0,0,0.15); }
    .stProgress > div > div > div > div { background-color: #d97757 !important; height: 6px !important; border-radius: 3px !important; }
    
    .ip-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(60px, 1fr)); gap: 8px; margin-top: 24px; padding: 20px; border-radius: 4px; border: 1px solid #ccc; }
    .ip-link { text-decoration: none; }
    .ip-box { padding: 12px 0; text-align: center; border-radius: 2px; font-size: 0.9rem; font-family: monospace, sans-serif !important; font-weight: 600; color: #ffffff !important; border: 1px solid rgba(0,0,0,0.2); cursor: pointer; transition: opacity 0.2s; }
    .ip-box:hover { opacity: 0.8; }
    .ip-taken { background-color: #c62828 !important; box-shadow: none !important; }
    .ip-free { background-color: #2e7d32 !important; box-shadow: none !important; opacity: 1 !important; }
    .ip-reserved { background-color: #f57f17 !important; box-shadow: none !important; } /* Orange/Yellow for Reserved */
    .ip-down { background-color: #424242 !important; box-shadow: none !important; } /* Grey for Down if used */
    """

    if mode == 'Light':
        return common_css + """
        .stApp { background-color: #fcfcf9 !important; color: #191919 !important; }
        header[data-testid="stHeader"] { background-color: #fcfcf9 !important; }
        header[data-testid="stHeader"] .st-emotion-cache-152e8e9 { color: #191919 !important; }
        [data-testid="stSidebar"] { background-color: #f4f3f0 !important; border-right: 1px solid #e5e5e0; }
        h1, h2, h3, p, div, span { color: #191919 !important; }
        .stButton > button { background-color: #ffffff !important; color: #191919 !important; border: 1px solid #e0e0e0 !important; box-shadow: 0 1px 2px rgba(0,0,0,0.05) !important; }
        .stButton > button:hover { background-color: #fafafa !important; border-color: #d0d0d0 !important; box-shadow: 0 4px 12px rgba(0,0,0,0.08) !important; }
        .ip-grid { background: #ffffff; box-shadow: 0 2px 10px rgba(0,0,0,0.03); border: 1px solid #f0f0f0; }
        .link-button { background-color: #191919 !important; color: #ffffff !important; border: 1px solid #191919; }
        .link-button:hover { background-color: #333333 !important; }
        .subnet-box { background-color: #f0f0f0; color: #191919; padding: 8px 12px; border-radius: 4px; border: 1px solid #e0e0e0; font-family: monospace; margin-bottom: 4px; }
        .ip-box { color: #ffffff !important; }
        """
    else: # Dark Mode
        return common_css + """
        .stApp { background-color: #121212 !important; color: #e0e0e0 !important; }
        header[data-testid="stHeader"] { background-color: #121212 !important; }
        header[data-testid="stHeader"] button { background-color: transparent !important; color: #e0e0e0 !important; }
        header[data-testid="stHeader"] svg { fill: #e0e0e0 !important; color: #e0e0e0 !important; }
        [data-testid="stDataFrame"] { filter: invert(0.9) hue-rotate(180deg) brightness(1.2); }
        .stProgress > div > div > div > div { background-color: #d97757 !important; }
        .stProgress > div > div > div { background-color: #333333 !important; }
        div[data-testid="stExpander"] { background-color: transparent !important; border: none !important; color: #e0e0e0 !important; }
        div[data-testid="stExpander"] details { background-color: #1e1e1e !important; border-radius: 4px; border: 1px solid #333; }
        div[data-testid="stExpander"] summary { background-color: #1e1e1e !important; color: #e0e0e0 !important; border-radius: 4px; }
        div[data-testid="stExpander"] summary:hover { background-color: #2d2d2d !important; color: #ffffff !important; }
        div[data-testid="stExpander"] summary p, div[data-testid="stExpander"] summary span, div[data-testid="stExpander"] summary div { color: #e0e0e0 !important; }
        div[data-baseweb="popover"] > div { background-color: #1a1a1a !important; color: #e0e0e0 !important; border: 1px solid #333; }
        div[data-baseweb="popover"] li, div[data-baseweb="popover"] div, div[data-baseweb="popover"] span, div[data-baseweb="popover"] p { color: #e0e0e0 !important; }
        div[data-baseweb="popover"] li:hover { background-color: #333 !important; }
        [data-testid="stSidebar"] { background-color: #1a1a1a !important; border-right: 1px solid #333; }
        h1, h2, h3, p, span, div { color: #e0e0e0 !important; }
        input, textarea { color: #e0e0e0 !important; background-color: transparent !important; }
        div[data-baseweb="input"] > div { background-color: #2d2d2d !important; color: #e0e0e0 !important; border-color: #444 !important; }
        div[data-baseweb="select"] > div { background-color: #2d2d2d !important; color: #e0e0e0 !important; border-color: #444 !important; }
        div[data-baseweb="popover"] ul, div[data-baseweb="menu"] ul, ul[data-testid="stSelectboxVirtualDropdown"] { background-color: #2d2d2d !important; }
        div[data-baseweb="popover"] li, div[data-baseweb="menu"] li, li[data-testid="stSelectboxVirtualDropdownOption"] { color: #e0e0e0 !important; }
        label { color: #e0e0e0 !important; }
        .stButton > button { background-color: #2d2d2d !important; color: #e0e0e0 !important; border: 1px solid #444 !important; }
        [data-testid="stFormSubmitButton"] > button { background-color: #2d2d2d !important; color: #e0e0e0 !important; border: 1px solid #444 !important; }
        .stButton > button p, [data-testid="stFormSubmitButton"] > button p { color: #e0e0e0 !important; }
        .stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover { background-color: #383838 !important; border-color: #666 !important; color: #ffffff !important; }
        .stButton > button:hover p, [data-testid="stFormSubmitButton"] > button:hover p { color: #ffffff !important; }
        .ip-grid { background: #1e1e1e; box-shadow: 0 2px 10px rgba(0,0,0,0.2); border: 1px solid #333; }
        .link-button { background-color: #e0e0e0 !important; color: #121212 !important; border: 1px solid #e0e0e0; }
        .link-button:hover { background-color: #ffffff !important; }
        .subnet-box { background-color: #2d2d2d; color: #e0e0e0; padding: 8px 12px; border-radius: 4px; border: 1px solid #444; font-family: monospace; margin-bottom: 4px; }
        hr { border-color: #444 !important; }
        .ip-box { color: #ffffff !important; }
        """

st.markdown(f"<style>{get_theme_css(st.session_state.theme)}</style>", unsafe_allow_html=True)

# --- DB Fetchers (Read-Only wrappers) ---
@st.cache_data(ttl=120, show_spinner=False)
def fetch_hosts_with_metrics():
    with database.SessionLocal() as db:
        hosts = db.query(ESXiHost).options(joinedload(ESXiHost.host_metrics)).all()
        results = []
        for h in hosts:
            metrics = h.host_metrics[0] if h.host_metrics else None
            results.append({
                "id": h.id, "ip": h.ip,
                "cpu_usage": metrics.cpu_usage if metrics else None,
                "used_cpu_ghz": metrics.used_cpu_ghz if metrics else 0,
                "total_cpu_ghz": metrics.total_cpu_ghz if metrics else 0,
                "mem_usage": metrics.mem_usage if metrics else None,
                "used_mem_gb": metrics.used_mem_gb if metrics else 0,
                "total_mem_gb": metrics.total_mem_gb if metrics else 0,
                "storage_usage": metrics.storage_usage if metrics else None,
                "used_storage_gb": metrics.used_storage_gb if metrics else 0,
                "total_storage_gb": metrics.total_storage_gb if metrics else 0,
                "last_updated": metrics.last_updated if metrics else None
            })
    return results

@st.cache_data(ttl=120, show_spinner=False)
def fetch_single_host_metrics(host_ip):
    with database.SessionLocal() as db:
        host = db.query(ESXiHost).filter_by(ip=host_ip).options(joinedload(ESXiHost.host_metrics)).first()
        if not host:
            return None
        metrics = host.host_metrics[0] if host.host_metrics else None
        result = {
            "id": host.id, "ip": host.ip,
            "cpu_usage": metrics.cpu_usage if metrics else None,
            "used_cpu_ghz": metrics.used_cpu_ghz if metrics else 0,
            "total_cpu_ghz": metrics.total_cpu_ghz if metrics else 0,
            "mem_usage": metrics.mem_usage if metrics else None,
            "used_mem_gb": metrics.used_mem_gb if metrics else 0,
            "total_mem_gb": metrics.total_mem_gb if metrics else 0,
            "storage_usage": metrics.storage_usage if metrics else None,
            "used_storage_gb": metrics.used_storage_gb if metrics else 0,
            "total_storage_gb": metrics.total_storage_gb if metrics else 0,
            "last_updated": metrics.last_updated if metrics else None
        }
    return result

@st.cache_data(ttl=120, show_spinner=False)
def fetch_vms_for_host(host_ip):
    with database.SessionLocal() as db:
        # Join to find host_id from ip
        host = db.query(ESXiHost).filter_by(ip=host_ip).options(joinedload(ESXiHost.vms)).first()
        if not host:
            return []

        vms = host.vms
        results = []
        for vm in vms:
            results.append({
                "name": vm.name, "os": vm.os, "ip": vm.ip,
                "cpu_count": vm.cpu_count, "ram_info": vm.ram_info,
                "disk_info": vm.disk_info, "created_date": vm.created_date,
                "power_state": vm.power_state
            })
    return results

@st.cache_data(ttl=120, show_spinner=False)
def fetch_all_vms(search_query=None, search_by="Name"):
    with database.SessionLocal() as db:
        query = db.query(VM).options(joinedload(VM.esxi_host))

        if search_query:
            if search_by == "Name":
                query = query.filter(VM.name.ilike(f"%{search_query}%"))
            elif search_by == "IP":
                query = query.filter(VM.ip.contains(search_query))

        vms = query.all()
        results = []
        for vm in vms:
            results.append({
                "name": vm.name, "os": vm.os, "ip": vm.ip,
                "host_ip": vm.esxi_host.ip,
                "cpu_count": vm.cpu_count,
                "ram_info": vm.ram_info, "disk_info": vm.disk_info,
                "created_date": vm.created_date, "power_state": vm.power_state
            })
    return results

def render_ip_map_page():
    st.title("🌐 IP Map")
    st.markdown("### Network Availability Map")
    
    col1, col2 = st.columns([3, 1])
    with col1:
        st.info("🟢 Green = Available | 🔴 Red = Taken (In Use) | 🟠 Orange = Reserved (Offline but Known)")
    with col2:
        if st.button("🔄 Scan ALL Zones", key="refresh_all_ips"):
            if data_collector.is_scan_running():
                st.warning("A subnet scan is already in progress.")
            else:
                import threading
                threading.Thread(target=data_collector.scan_all_subnets, daemon=True).start()
                st.toast("Subnet scan started in the background.")
            st.rerun()

    # --- Subnet Management ---
    with st.expander("⚙️ Manage Subnets"):
        m_col1, m_col2 = st.columns([1, 2])
        with m_col1:
            with st.form("add_subnet_form", clear_on_submit=True):
                new_subnet = st.text_input("Add Subnet (e.g., 192.168.50)", help="Enter the first 3 octets")
                if st.form_submit_button("Add"):
                    if new_subnet and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", new_subnet):
                        if database.add_subnet(new_subnet):
                            st.success(f"Added {new_subnet}")
                            st.rerun()
                        else:
                            st.error("Subnet already exists.")
                    else:
                        st.error("Invalid format. Use x.x.x")
        
        with m_col2:
            st.write("Configured Subnets:")
            current_subnets = database.get_all_subnets()
            if current_subnets:
                for s in current_subnets:
                    c1, c2 = st.columns([4, 1])
                    c1.markdown(f'<div class="subnet-box">{s}</div>', unsafe_allow_html=True)
                    if c2.button("🗑️", key=f"del_{s}"):
                        database.remove_subnet(s)
                        st.rerun()
            else:
                st.info("No subnets configured.")

    # --- State Management & URL Sync ---
    available_subnets = current_subnets
    if not available_subnets:
        st.warning("No subnets configured. Please add a subnet above.")
        return

    query_params = st.query_params
    qp_subnet = query_params.get("subnet", None)

    if qp_subnet in available_subnets:
        if st.session_state.get("selected_subnet") != qp_subnet:
            st.session_state.selected_subnet = qp_subnet

    if "selected_subnet" not in st.session_state or st.session_state.selected_subnet not in available_subnets:
        st.session_state.selected_subnet = available_subnets[0]

    selected_subnet = st.selectbox(
        "Select Zone to View:", 
        available_subnets, 
        index=available_subnets.index(st.session_state.selected_subnet),
        key="subnet_selector"
    )
    
    if selected_subnet != st.session_state.selected_subnet:
        st.session_state.selected_subnet = selected_subnet
        if "inspect_ip" in st.query_params:
            del st.query_params["inspect_ip"]
        st.query_params["subnet"] = selected_subnet
        st.rerun()
    
    if st.query_params.get("subnet") != selected_subnet:
        st.query_params["subnet"] = selected_subnet

    # --- Load Data from DB ---
    with database.SessionLocal() as db:
        leases = db.query(IPLease).filter_by(subnet=selected_subnet).all()
        # Map IP to status
        ip_status_map = {lease.ip: lease.status for lease in leases}

    # --- Inspection Logic ---
    inspect_ip = query_params.get("inspect_ip", None)

    if inspect_ip:
        if inspect_ip.startswith(selected_subnet):
            st.divider()
            st.subheader(f"Details for {inspect_ip}")
            
            # DB Search for VM
            found_vms = fetch_all_vms(inspect_ip, "IP")
            
            if found_vms:
                for vm in found_vms:
                    state_raw = vm.get('power_state', '')
                    if "poweredOn" in state_raw:
                        state_icon = "🟢 ↑"
                    elif "poweredOff" in state_raw:
                        state_icon = "🔴 ↓"
                    else:
                        state_icon = f"⚪ {state_raw}"
                    
                    st.success(f"Found VM: {vm['name']} {state_icon}")
                    col1, col2 = st.columns(2)
                    with col1:
                        st.write(f"**OS:** {vm['os']}")
                        st.write(f"**CPU:** {vm['cpu_count']} vCPUs")
                        st.write(f"**RAM:** {vm['ram_info']}")
                    with col2:
                        st.write(f"**Host:** {vm['host_ip']}")
                        st.write(f"**Disks:**")
                        st.text(vm['disk_info'])
                    
                    if st.button(f"Go to Host {vm['host_ip']}", key=f"btn_host_{inspect_ip}"):
                        st.session_state.host = vm['host_ip']
                        st.session_state.page = 'dashboard'
                        st.query_params["page"] = "dashboard"
                        st.rerun()
            else:
                with database.SessionLocal() as db:
                    lease = db.query(IPLease).filter_by(ip=inspect_ip).first()
                    current_status = lease.status if lease else IPStatus.FREE
                    last_updated = lease.last_updated if lease else None
                
                st.info(f"Status: {current_status.value}")
                if last_updated:
                    st.caption(f"Last Status Change: {last_updated.strftime('%Y-%m-%d %H:%M:%S')}")

                if current_status == IPStatus.RESERVED:
                    st.warning("This IP is reserved but offline. You can manually unreserve it if the device is no longer in use.")
                    if st.button("Unreserve IP", type="primary", use_container_width=True):
                        with database.SessionLocal() as db:
                            lease = db.query(IPLease).get(inspect_ip)
                            if lease:
                                lease.status = IPStatus.FREE
                                lease.last_updated = datetime.now()
                                
                                log = HistoryLog(
                                    timestamp=datetime.now(),
                                    ip=inspect_ip,
                                    status=IPStatus.FREE,
                                    device_id=lease.device_id,
                                    hostname_snapshot="Manual Unreserve"
                                )
                                db.add(log)
                                db.commit()
                                st.success(f"IP {inspect_ip} has been unreserved.")
                                time.sleep(1)
                                st.rerun()
                
                # Check history
                st.markdown("#### Historical Activity")
                with database.SessionLocal() as db:
                    history = db.query(HistoryLog).filter_by(ip=inspect_ip).order_by(HistoryLog.timestamp.desc()).limit(5).all()
                    if history:
                        hist_data = [{"Time": h.timestamp, "State": h.status.value, "Device": h.hostname_snapshot or "Unknown"} for h in history]
                        st.data_editor(hist_data, use_container_width=True, disabled=True)
                    else:
                        st.text("No history recorded.")

            if st.button("Close Details"):
                if "inspect_ip" in st.query_params:
                    del st.query_params["inspect_ip"]
                st.rerun()
            st.divider()
        else:
            if "inspect_ip" in st.query_params:
                del st.query_params["inspect_ip"]
            st.rerun()

    # --- Grid Rendering ---
    grid_html = '<div class="ip-grid">'
    for i in range(256):
        current_ip = f"{selected_subnet}.{i}"
        
        status = ip_status_map.get(current_ip, IPStatus.FREE)
        tooltip = f"{current_ip} ({status.value})"
        
        status_class = "ip-free"
        if status == IPStatus.ACTIVE:
            status_class = "ip-taken"
        elif status == IPStatus.RESERVED:
            status_class = "ip-reserved"
        elif status == IPStatus.DOWN:
            status_class = "ip-down"

        current_theme = st.session_state.get('theme', 'Light')
        link = f"?page=ip_management&subnet={selected_subnet}&inspect_ip={current_ip}&theme={current_theme}"
        grid_html += f'<a href="{link}" target="_self" class="ip-link"><div class="ip-box {status_class}" title="{tooltip}">{i}</div></a>'
    
    grid_html += '</div>'
    st.markdown(grid_html, unsafe_allow_html=True)

def render_history_page():
    st.title("\U0001f570\ufe0f History / DR")
    
    st.markdown("### Recover Network State")
    
    col1, col2 = st.columns(2)
    with col1:
        target_date = st.date_input("Select Date", value=datetime.now())
    with col2:
        target_time = st.time_input("Select Time", value=datetime.now().time())
    
    target_datetime = datetime.combine(target_date, target_time)
    
    if st.button("Query History"):
        with database.SessionLocal() as db:
            # Find the latest log for each IP before or at target_datetime
            subquery = db.query(
                HistoryLog.ip,
                func.max(HistoryLog.timestamp).label('max_ts')
            ).filter(HistoryLog.timestamp <= target_datetime).group_by(HistoryLog.ip).subquery()
            
            # Strict Infrastructure Filter: Only show VMs linked to known ESXi hosts
            # Path: HistoryLog -> NetworkDevice -> VM (name match) -> ESXiHost
            # INNER JOINs enforce that we only see records with a complete chain of ownership
            results = db.query(HistoryLog, ESXiHost.ip).select_from(HistoryLog).join(
                subquery, 
                (HistoryLog.ip == subquery.c.ip) & (HistoryLog.timestamp == subquery.c.max_ts)
            ).join(
                NetworkDevice, HistoryLog.device_id == NetworkDevice.id
            ).join(
                VM, NetworkDevice.hostname == VM.name
            ).join(
                ESXiHost, VM.host_id == ESXiHost.id
            ).filter(
                HistoryLog.status != IPStatus.FREE,
                HistoryLog.ip.like('192.168.%')
            ).order_by(
                ESXiHost.ip.asc(),
                HistoryLog.ip.asc()
            ).all()
        
        if results:
            logs = [r[0] for r in results]
            actual_max_ts = max(log.timestamp for log in logs)
            st.success(f"Restored view for request: {target_datetime}")
            st.info(f"Most recent data point in this view: {actual_max_ts}")
            
            data = []
            for log, host_ip in results:
                data.append({
                    "Physical Server": host_ip,
                    "VM Name": log.hostname_snapshot,
                    "IP Address": log.ip,
                    "Last Status": log.status.value,
                    "Snapshot Time": log.timestamp
                })
            
            df = pd.DataFrame(data)
            
            # Already sorted in SQL
            
            # Reorder columns explicitly to ensure Physical Server is first
            df = df[["Physical Server", "VM Name", "IP Address", "Last Status", "Snapshot Time"]]
            
            # Display
            st.dataframe(df, use_container_width=True)
            
            # Export Logic
            csv_df = df.copy()
            csv_df["[ ] Recovered?"] = "" # Empty checklist column
            csv = csv_df.to_csv(index=False).encode('utf-8')
            
            st.download_button(
                label="📥 Export Checklist to CSV",
                data=csv,
                file_name=f"recovery_plan_{target_datetime.strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv"
            )
            
        else:
            st.info("No history found for this timestamp. Try running a 'Scan ALL Zones' to generate a baseline.")

@st.cache_data(ttl=120, show_spinner=False)
def fetch_recent_vms(start_date_str, end_date_str):
    """Fetch VMs created within the given date range, with SQL-level filtering."""
    with database.SessionLocal() as db:
        vms = db.query(VM).options(joinedload(VM.esxi_host)).filter(
            VM.created_date.isnot(None),
            VM.created_date >= start_date_str,
            VM.created_date <= end_date_str + "T23:59:59"
        ).all()
        results = []
        for vm in vms:
            results.append({
                "VM IP": vm.ip,
                "ESXi Host": vm.esxi_host.ip if vm.esxi_host else "Unknown",
                "Name": vm.name,
                "Created": vm.created_date,
                "RAM": vm.ram_info,
                "CPU": vm.cpu_count,
                "Storage": vm.disk_info,
                "State": vm.power_state
            })
    return results

def render_recent_vms_page():
    st.title("\U0001f552 Recently Created")

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date", value=datetime.now() - timedelta(days=7))
    with col2:
        end_date = st.date_input("End Date", value=datetime.now())

    if start_date > end_date:
        st.error("Error: End date must fall after start date.")
        return

    found_vms = fetch_recent_vms(start_date.isoformat(), end_date.isoformat())

    if found_vms:
        st.success(f"Found {len(found_vms)} VMs.")
        st.data_editor(found_vms, use_container_width=True, disabled=True)
    else:
        st.info("No VMs found in DB matching this range.")

def get_color_from_percentage(percentage):
    if percentage > 90: return "red"
    if percentage > 70: return "orange"
    return "green"

def display_host_details(host_ip):
    col1, col2 = st.columns([5, 1])
    with col1:
        st.header(f"🖥️ Details for {host_ip}")
    with col2:
        st.markdown(f'<a href="https://{host_ip}" target="_blank" class="link-button">OPEN ESXi WEB CLIENT</a>', unsafe_allow_html=True)
        if st.button("← BACK TO HUB", key=f"back_details_{host_ip}", use_container_width=True):
            st.session_state.host = None
            st.rerun()

    host_data = fetch_single_host_metrics(host_ip)

    st.subheader("Resource Usage (Cached)")
    if host_data and host_data['cpu_usage'] is not None:
        metrics = host_data
        cpu_usage, mem_usage, storage_usage = metrics['cpu_usage'], metrics['mem_usage'], metrics['storage_usage']
        cpu_color, mem_color, storage_color = get_color_from_percentage(cpu_usage), get_color_from_percentage(mem_usage), get_color_from_percentage(storage_usage)
        
        st.markdown(f"**CPU:** {metrics['used_cpu_ghz']:.2f}/{metrics['total_cpu_ghz']:.2f} GHz (<span style='color:{cpu_color}; font-weight:bold;'>{cpu_usage:.2f}%</span>)", unsafe_allow_html=True)
        st.progress(int(cpu_usage))
        st.markdown(f"**Memory:** {metrics['used_mem_gb']:.2f}/{metrics['total_mem_gb']:.2f} GB (<span style='color:{mem_color}; font-weight:bold;'>{mem_usage:.2f}%</span>)", unsafe_allow_html=True)
        st.progress(int(mem_usage))
        st.markdown(f"**Storage:** {metrics['used_storage_gb']:.2f}/{metrics['total_storage_gb']:.2f} GB (<span style='color:{storage_color}; font-weight:bold;'>{storage_usage:.2f}%</span>)", unsafe_allow_html=True)
        st.progress(int(storage_usage))
        st.caption(f"Last updated: {metrics['last_updated']}")
    else:
        st.warning("No metrics available in DB. Please refresh data.")

    st.subheader("Virtual Machines")
    vms = fetch_vms_for_host(host_ip)

    if vms:
        search_query = st.text_input("Search for a VM by name:", key=f"search_{host_ip}")
        if search_query:
            vms = [vm for vm in vms if search_query.lower() in vm["name"].lower()]
        
        display_vms = []
        for vm in vms:
            state_raw = str(vm['power_state'])
            if "poweredOn" in state_raw: state_display = "🟢 ↑"
            elif "poweredOff" in state_raw: state_display = "🔴 ↓"
            else: state_display = f"⚪ {state_raw}"

            display_vms.append({
                "Name": vm['name'], "OS": vm['os'], "IP": vm['ip'],
                "CPU (vCPUs)": vm['cpu_count'], "RAM": vm['ram_info'],
                "Disks": vm['disk_info'], "Created": vm['created_date'],
                "State": state_display
            })
        st.data_editor(display_vms, use_container_width=True, disabled=True)
    else: st.info("No VMs found on this host in DB.")

def user_management(users_config, username):
    st.title("\u2699\ufe0f User Management")
    st.subheader("Add New User")
    with st.form("add_user_form", clear_on_submit=True):
        new_username = st.text_input("Username")
        new_name = st.text_input("Name")
        new_email = st.text_input("Email")
        new_password = st.text_input("Password", type="password")
        new_role = st.selectbox("Role", ["admin", "user"], index=1)
        if st.form_submit_button("Add User"):
            if new_username and new_password:
                hashed_password = stauth.Hasher.hash(new_password)
                users_config['credentials']['usernames'][new_username] = {'email': new_email, 'name': new_name, 'password': hashed_password, 'role': new_role}
                with open('./users.json', 'w') as file: json.dump(users_config, file, indent=4)
                _load_users_config.clear()
                st.success(f"User {new_username} added successfully!")
            else: st.error("Username and Password cannot be empty.")

    st.subheader("Update Existing User / Change Password")
    current_usernames = list(users_config['credentials']['usernames'].keys())
    selected_username = st.selectbox("Select User to Update", current_usernames)
    if selected_username:
        user_data = users_config['credentials']['usernames'][selected_username]
        with st.form(f"update_user_form_{selected_username}"):
            updated_name = st.text_input("Name", value=user_data.get('name', ''))
            updated_email = st.text_input("Email", value=user_data.get('email', ''))
            new_password_update = st.text_input("New Password (leave blank to keep current)", type="password")
            updated_role = st.selectbox("Role", ["admin", "user"], index=0 if user_data.get('role') == 'admin' else 1)
            if st.form_submit_button("Update User"):
                user_data['name'] = updated_name
                user_data['email'] = updated_email
                user_data['role'] = updated_role
                if new_password_update: user_data['password'] = stauth.Hasher.hash(new_password_update)
                with open('./users.json', 'w') as file: json.dump(users_config, file, indent=4)
                _load_users_config.clear()
                st.success(f"User {selected_username} updated successfully!")

    st.subheader("Delete User")
    with st.form("delete_user_form", clear_on_submit=True):
        user_to_delete = st.selectbox("Select User to Delete", [u for u in current_usernames if u != username])
        if st.form_submit_button("Delete User"):
            if user_to_delete:
                del users_config['credentials']['usernames'][user_to_delete]
                with open('./users.json', 'w') as file: json.dump(users_config, file, indent=4)
                _load_users_config.clear()
                st.success(f"User {user_to_delete} deleted successfully!")
                st.rerun()
            else: st.error("Please select a user to delete.")
    if st.button("Back to Dashboard"):
        st.session_state.page = 'dashboard'
        st.rerun()

@st.cache_data(show_spinner=False)
def _load_users_config():
    with open('./users.json') as file:
        return json.load(file)

@st.fragment(run_every="60s")
def render_dashboard_grid(sort_by, sort_desc):
    all_hosts_with_metrics = fetch_hosts_with_metrics()
    
    if sort_by != "Default":
        def get_sort_key(h):
            metric_key = f"{sort_by.lower()}_usage"
            if sort_by == "Memory": metric_key = "mem_usage"
            val = h[metric_key]
            return val if val is not None else -1
        all_hosts_with_metrics = sorted(all_hosts_with_metrics, key=get_sort_key, reverse=sort_desc)
    elif sort_desc:
        all_hosts_with_metrics = sorted(all_hosts_with_metrics, key=lambda x: x['ip'], reverse=True)

    num_columns = 3
    cols = st.columns(num_columns)
    
    for i, host_data in enumerate(all_hosts_with_metrics):
        with cols[i % num_columns]:
            with st.container(border=True):
                st.subheader(f"🖥️ {host_data['ip']}")
                if host_data['cpu_usage'] is None: st.warning("No data available.")
                else:
                    metrics = host_data
                    cpu_usage, mem_usage, storage_usage = metrics['cpu_usage'], metrics['mem_usage'], metrics['storage_usage']
                    cpu_color, mem_color, storage_color = get_color_from_percentage(cpu_usage), get_color_from_percentage(mem_usage), get_color_from_percentage(storage_usage)

                    st.markdown(f"**CPU:** {metrics['used_cpu_ghz']:.2f}/{metrics['total_cpu_ghz']:.2f} GHz (<span style='color:{cpu_color}; font-weight:bold;'>{cpu_usage:.2f}%</span>)", unsafe_allow_html=True)
                    st.progress(int(cpu_usage))
                    st.markdown(f"**Memory:** {metrics['used_mem_gb']:.2f}/{metrics['total_mem_gb']:.2f} GB (<span style='color:{mem_color}; font-weight:bold;'>{mem_usage:.2f}%</span>)", unsafe_allow_html=True)
                    st.progress(int(mem_usage))
                    st.markdown(f"**Storage:** {metrics['used_storage_gb']:.2f}/{metrics['total_storage_gb']:.2f} GB (<span style='color:{storage_color}; font-weight:bold;'>{storage_usage:.2f}%</span>)", unsafe_allow_html=True)
                    st.progress(int(storage_usage))

                b_col1, b_col2 = st.columns(2)
                with b_col1:
                    if st.button("DETAILS", key=f"btn_details_{host_data['ip']}"):
                        st.session_state.host = host_data['ip']
                        st.rerun()
                with b_col2: st.markdown(f'<a href="https://{host_data["ip"]}" target="_blank" class="link-button">OPEN</a>', unsafe_allow_html=True)

def main():
    try:
        users_config = _load_users_config()
    except:
        st.error("users.json not found.")
        st.stop()

    authenticator = stauth.Authenticate(
        users_config['credentials'], users_config['cookie']['name'],
        users_config['cookie']['key'], users_config['cookie']['expiry_days']
    )

    header_col1, header_col2 = st.columns([1, 10])
    with header_col1:
        try: st.image("image.png", width=60)
        except: pass
    with header_col2: st.markdown("<h2 style='margin-top: 10px;'>ESXi Monitoring Dashboard</h2>", unsafe_allow_html=True)
    
    authenticator.login(location='main')
    authentication_status = st.session_state.get('authentication_status')
    username = st.session_state.get('username')

    if authentication_status == False:
        st.error('Username/password is incorrect')
        st.stop()
    elif authentication_status == None:
        st.warning('Please enter your username and password')
        st.stop()

    st.session_state['role'] = users_config['credentials']['usernames'][username]['role']

    # Router
    if "page" in st.query_params and st.session_state.get("page") != st.query_params["page"]:
        st.session_state.page = st.query_params["page"]
    if 'page' not in st.session_state: st.session_state.page = 'dashboard'
    
    with st.sidebar:
        st.title("Menu")
        is_dark = (st.session_state.theme == 'Dark')
        theme_val = st.toggle("Dark Mode", value=is_dark)
        if theme_val != is_dark:
            new_theme = "Dark" if theme_val else "Light"
            st.session_state.theme = new_theme
            st.query_params["theme"] = new_theme
            st.rerun()
            
        st.divider()
        if st.button("📊 Dashboard", use_container_width=True):
            st.session_state.page = 'dashboard'
            st.session_state.host = None
            st.session_state.found_vms = None
            st.query_params.clear()
            st.query_params["page"] = "dashboard"
            st.query_params["theme"] = st.session_state.theme
            st.rerun()
        if st.button("🌐 IP Map", use_container_width=True):
            st.session_state.page = 'ip_management'
            st.query_params.clear()
            st.query_params["page"] = "ip_management"
            st.query_params["theme"] = st.session_state.theme
            st.rerun()
        if st.button("🕒 Recently Created", use_container_width=True):
            st.session_state.page = 'recent_vms'
            st.query_params.clear()
            st.query_params["page"] = "recent_vms"
            st.query_params["theme"] = st.session_state.theme
            st.rerun()
        if st.button("🕰️ History / DR", use_container_width=True): # New Button
            st.session_state.page = 'history'
            st.query_params.clear()
            st.query_params["page"] = "history"
            st.query_params["theme"] = st.session_state.theme
            st.rerun()

        if st.button("🧠 AI Infrastructure Agent", use_container_width=True):
            st.session_state.page = 'ai_agent'
            st.query_params.clear()
            st.query_params["page"] = "ai_agent"
            st.query_params["theme"] = st.session_state.theme
            st.rerun()

        # --- Gemini API Configuration (visible when on AI agent page) ---
        if st.session_state.get("page") == "ai_agent":
            st.session_state.ai_backend = "gemini"
            
            # Allow user to provide their own key
            user_key = st.sidebar.text_input(
                "Gemini API Key",
                type="password",
                value=st.session_state.get("gemini_api_key", ""),
                help="Enter your Google AI Studio API key. It is not stored permanently."
            )
            if user_key:
                st.session_state.gemini_api_key = user_key
            
            if not st.session_state.get("gemini_api_key") and not os.getenv("GEMINI_API_KEY"):
                st.warning("Please provide a Gemini API Key in the sidebar.")

        if st.session_state.get('role') == 'admin':
            if st.button("⚙️ User Management", use_container_width=True):
                st.session_state.page = 'user_management'
                st.query_params.clear()
                st.query_params["page"] = "user_management"
                st.query_params["theme"] = st.session_state.theme
                st.rerun()
        
        st.divider()
        authenticator.logout('🚪 LOGOUT', location='sidebar')

        # --- Data Collection Settings ---
        st.divider()
        _interval_options = {
            "1 min": 60, "5 min": 300, "10 min": 600,
            "20 min": 1200, "30 min": 1800, "1 hour": 3600,
        }
        _interval_labels = list(_interval_options.keys())
        _interval_values = list(_interval_options.values())
        # Read from session_state cache (avoids DB hit every rerun)
        if "_cached_interval" not in st.session_state:
            st.session_state._cached_interval = background_job.get_interval_seconds()
        _current_idx = _interval_values.index(st.session_state._cached_interval) if st.session_state._cached_interval in _interval_values else 1
        selected_label = st.selectbox(
            "Collection Interval",
            options=_interval_labels,
            index=_current_idx,
            key="collection_interval_select",
        )
        _selected_secs = _interval_options[selected_label]
        if _selected_secs != st.session_state._cached_interval:
            background_job.set_interval_seconds(_selected_secs)
            st.session_state._cached_interval = _selected_secs

        # Collection status (reads from in-memory dict, no DB hit)
        _status = background_job.get_status()
        if _status["collecting"]:
            st.caption("🔄 Collecting data...")
        elif _status["last_run"]:
            st.caption(f"Last update: {_status['last_run'].strftime('%H:%M:%S')}")

        if st.button("🔄 Refresh Data", use_container_width=True):
             st.cache_data.clear()
             st.rerun()

    if st.session_state.page == 'user_management':
        user_management(users_config, username)
    elif st.session_state.page == 'ip_management':
        render_ip_map_page()
    elif st.session_state.page == 'recent_vms':
        render_recent_vms_page()
    elif st.session_state.page == 'history':
        render_history_page()
    elif st.session_state.page == 'ai_agent':
        ai_agent.render_ai_agent()
    else: # Dashboard page
        if 'host' not in st.session_state: st.session_state.host = None
        
        if st.session_state.host:
            display_host_details(st.session_state.host)
        else:
            search_by = st.selectbox("Search by:", ["Name", "IP"], key="search_by")
            query = st.text_input(f"Enter VM {search_by} to find its ESXi host:", key="vm_search")

            if query: st.session_state.found_vms = fetch_all_vms(query, search_by)
            else: st.session_state.found_vms = None

            if st.session_state.found_vms:
                st.success(f"Found {len(st.session_state.found_vms)} VMs matching your query:")
                for i, vm in enumerate(st.session_state.found_vms):
                    col1, col2 = st.columns([3, 1])
                    with col1: st.write(f"**VM Name:** {vm['name']} | **VM IP:** {vm['ip']} | **ESXi Host:** {vm['host_ip']}")
                    with col2:
                        if st.button("View Host", key=f"view_host_{i}_{vm['name']}"):
                            st.session_state.host = vm['host_ip']
                            st.session_state.found_vms = None
                            st.rerun()
            elif query and not st.session_state.found_vms: st.error(f"No VMs found matching '{query}'.")

            st.header("\U0001f4ca Dashboard")
            sort_col1, sort_col2 = st.columns(2)
            sort_by = sort_col1.selectbox("Sort by:", ["Default", "CPU", "Memory", "Storage"], key="host_sort_by")
            with sort_col2:
                st.markdown("<div style='height: 29px;'></div>", unsafe_allow_html=True)
                sort_desc = st.checkbox("Descending", key="host_sort_desc")

            render_dashboard_grid(sort_by, sort_desc)

if __name__ == "__main__":
    main()