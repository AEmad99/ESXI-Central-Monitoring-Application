import ssl
import re
import streamlit as st
import requests
import streamlit_authenticator as stauth
import json
import platform
import subprocess
from concurrent.futures import ThreadPoolExecutor
import time
import os
import atexit
from datetime import datetime, timedelta
import pandas as pd # Added for easier DB to DF conversion

# --- New Modules ---
import db_manager
import data_collector
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Disable SSL warnings for self-signed certificates
requests.packages.urllib3.disable_warnings()

st.set_page_config(layout="wide", page_title="ESXi Monitoring Dashboard", initial_sidebar_state="collapsed")

# --- Database Initialization & Seeding ---
# --- Database Initialization & Seeding ---
# Host Groups (Loaded from .env JSON)
try:
    host_groups_json = os.getenv("HOST_GROUPS_JSON", "{}")
    # Handle single quotes wrapping the JSON string if present from .env loading quirks
    if host_groups_json.startswith("'") and host_groups_json.endswith("'"):
        host_groups_json = host_groups_json[1:-1]
        
    raw_groups = json.loads(host_groups_json)
    HOST_GROUPS = {}
    for group_name, data in raw_groups.items():
        # Resolve password from env var name
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

# Ensure DB is ready
# We run this unconditionally to ensure schema updates (like adding new tables) apply on reload
db_manager.init_db()
# Sync DB with current config (updates passwords/users if changed)
db_manager.update_hosts_from_config(HOST_GROUPS)
db_manager.seed_hosts_if_empty(HOST_GROUPS)
db_manager.seed_subnets_if_empty()
st.session_state.db_initialized = True



# --- Theme Management ---
if 'theme' not in st.session_state:
    st.session_state.theme = 'Light'

def get_theme_css(mode):
    # Common Styles
    common_css = """
    /* Design System Common */
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Outfit', sans-serif !important;
    }
    
    /* Headers */
    h1, h2, h3 {
        font-weight: 500 !important;
        letter-spacing: -0.02em !important;
    }
    
    /* Buttons */
    .stButton > button {
        border-radius: 8px !important;
        font-weight: 500 !important;
        transition: all 0.2s cubic-bezier(0.2, 0, 0, 1) !important;
    }
    
    .stButton > button:hover {
        transform: translateY(-2px);
    }
    
    /* Link Buttons */
    .link-button {
        text-decoration: none !important;
        padding: 0.6rem 1.2rem;
        border-radius: 8px !important;
        text-align: center;
        cursor: pointer;
        display: block;
        width: 100%;
        box-sizing: border-box;
        font-size: 0.9rem;
        font-weight: 500;
        transition: all 0.2s ease;
        box-shadow: 0 2px 6px rgba(0,0,0,0.1);
    }
    .link-button:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 12px rgba(0,0,0,0.15);
    }
    
    /* Progress Bars */
    .stProgress > div > div > div > div {
        background-color: #d97757 !important; /* Terracotta accent */
        height: 6px !important;
        border-radius: 3px !important;
    }
    
    /* Grid & Animations */
    /* Grid & Simple Classic Styling */
    .ip-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(60px, 1fr));
        gap: 8px;
        margin-top: 24px;
        padding: 20px;
        border-radius: 4px;
        border: 1px solid #ccc;
    }
    .ip-link { text-decoration: none; }
    
    .ip-box {
        padding: 12px 0;
        text-align: center;
        border-radius: 2px; 
        font-size: 0.9rem;
        font-family: monospace, sans-serif !important; 
        font-weight: 600;
        color: #ffffff !important;
        border: 1px solid rgba(0,0,0,0.2);
        cursor: pointer;
        transition: opacity 0.2s;
    }
    
    .ip-box:hover {
        opacity: 0.8;
    }
    
    .ip-taken {
        background-color: #c62828 !important; /* Solid Classic Red */
        box-shadow: none !important;
    }
    .ip-free {
        background-color: #2e7d32 !important; /* Solid Classic Green */
        box-shadow: none !important;
        opacity: 1 !important;
    }

    """

    if mode == 'Light':
        return common_css + """
        /* Light Mode Main Background */
        .stApp {
            background-color: #fcfcf9 !important;
            color: #191919 !important;
        }
        
        /* Header Background - Light */
        header[data-testid="stHeader"] {
            background-color: #fcfcf9 !important;
        }
        /* Header Toolbar Icons - Light */
        header[data-testid="stHeader"] .st-emotion-cache-152e8e9 {
            color: #191919 !important;
        }
        
        [data-testid="stSidebar"] {
            background-color: #f4f3f0 !important;
            border-right: 1px solid #e5e5e0;
        }

        h1, h2, h3, p, div, span { color: #191919 !important; }
        
        .stButton > button {
            background-color: #ffffff !important;
            color: #191919 !important;
            border: 1px solid #e0e0e0 !important;
            box-shadow: 0 1px 2px rgba(0,0,0,0.05) !important;
        }
        .stButton > button:hover {
            background-color: #fafafa !important;
            border-color: #d0d0d0 !important;
            box-shadow: 0 4px 12px rgba(0,0,0,0.08) !important;
        }
        .ip-grid {
            background: #ffffff;
            box-shadow: 0 2px 10px rgba(0,0,0,0.03);
            border: 1px solid #f0f0f0;
        }
        .link-button {
            background-color: #191919 !important; 
            color: #ffffff !important;
            border: 1px solid #191919;
        }
        .link-button:hover {
            background-color: #333333 !important;
        }
        /* Exceptions for specific elements that need light text */
        .ip-box { color: #ffffff !important; }
        """
    else: # Dark Mode
        return common_css + """
        /* Dark Mode Main Background */
        .stApp {
            background-color: #121212 !important;
            color: #e0e0e0 !important;
        }
        
        /* Header Background - Dark */
        header[data-testid="stHeader"] {
            background-color: #121212 !important;
        }
        
        /* Toolbar Buttons - Dark */
        header[data-testid="stHeader"] button {
            background-color: transparent !important;
            color: #e0e0e0 !important;
        }
        
        /* Toolbar Icons - specifically target SVGs to avoid filling bounding boxes */
        header[data-testid="stHeader"] svg {
            fill: #e0e0e0 !important;
            color: #e0e0e0 !important;
        }
        
        /* Force DataFrame to fit Dark Theme via inversion */
        [data-testid="stDataFrame"] {
            filter: invert(1) hue-rotate(180deg);
        }


        /* Dark Mode Popover/Menu Background Fix */
        div[data-baseweb="popover"] > div {
            background-color: #1a1a1a !important;
            color: #e0e0e0 !important;
            border: 1px solid #333;
        }
        /* Fix text colors inside popover */
        div[data-baseweb="popover"] li, 
        div[data-baseweb="popover"] div, 
        div[data-baseweb="popover"] span,
        div[data-baseweb="popover"] p {
            color: #e0e0e0 !important;
        }
        /* Hover state for menu items - messy to target exact classes, but try generic list item hover */
        div[data-baseweb="popover"] li:hover {
            background-color: #333 !important;
        }

        [data-testid="stSidebar"] {
            background-color: #1a1a1a !important;
            border-right: 1px solid #333;
        }


        /* Force text color for headers and standard text elements */
        h1, h2, h3, p, span, div { color: #e0e0e0 !important; }
        
        /* Dark Mode Inputs/Text/Cards override */
        div[data-baseweb="input"] > div, input {
            background-color: #2d2d2d !important;
            color: #e0e0e0 !important;
            border-color: #444 !important;
        }
        div[data-baseweb="select"] > div {
            background-color: #2d2d2d !important;
            color: #e0e0e0 !important;
            border-color: #444 !important;
        }
        
        /* Fix label colors (like "Search by") */
        label { color: #e0e0e0 !important; }
        
        .stButton > button {
            background-color: #2d2d2d !important;
            color: #e0e0e0 !important;
            border: 1px solid #444 !important;
        }
        .stButton > button:hover {
            background-color: #383838 !important;
            border-color: #666 !important;
        }
        
        .ip-grid {
            background: #1e1e1e;
            box-shadow: 0 2px 10px rgba(0,0,0,0.2);
            border: 1px solid #333;
        }
        
        .link-button {
            background-color: #e0e0e0 !important; 
            color: #121212 !important;
            border: 1px solid #e0e0e0;
        }
        .link-button:hover {
            background-color: #ffffff !important;
        }
        
        /* Divider color */
        hr { border-color: #444 !important; }
        
        /* Exceptions */
        .ip-box { color: #ffffff !important; }
        """

st.markdown(f"<style>{get_theme_css(st.session_state.theme)}</style>", unsafe_allow_html=True)

# --- UI Helper Functions ---
def get_server_os_info():
    """Detects the underlying OS of the Streamlit server."""
    return f"{platform.system()} {platform.release()}"

def get_color_from_percentage(percentage):
    """Returns a color based on the resource usage percentage."""
    if percentage > 90:
        return "red"
    if percentage > 70:
        return "orange"
    return "green"

# --- DB Fetchers (Read-Only wrappers) ---
def fetch_hosts_with_metrics():
    conn = db_manager.get_db_connection()
    # Left join to get all hosts even if no metrics yet
    query = """
    SELECT h.id, h.ip, hm.cpu_usage, hm.used_cpu_ghz, hm.total_cpu_ghz, 
           hm.mem_usage, hm.used_mem_gb, hm.total_mem_gb, 
           hm.storage_usage, hm.used_storage_gb, hm.total_storage_gb, hm.last_updated
    FROM hosts h
    LEFT JOIN host_metrics hm ON h.id = hm.host_id
    """
    hosts = conn.execute(query).fetchall()
    conn.close()
    return hosts

def fetch_vms_for_host(host_ip):
    conn = db_manager.get_db_connection()
    # Join to find host_id from ip
    query = """
    SELECT v.* 
    FROM vms v
    JOIN hosts h ON v.host_id = h.id
    WHERE h.ip = ?
    ORDER BY v.name
    """
    vms = conn.execute(query, (host_ip,)).fetchall()
    conn.close()
    return vms

def fetch_all_vms(search_query=None, search_by="Name"):
    conn = db_manager.get_db_connection()
    base_query = """
    SELECT v.*, h.ip as host_ip 
    FROM vms v
    JOIN hosts h ON v.host_id = h.id
    """
    vms = conn.execute(base_query).fetchall()
    conn.close()
    
    results = []
    for vm in vms:
        match = False
        if not search_query:
            match = True
        elif search_by == "Name" and search_query.lower() in vm['name'].lower():
            match = True
        elif search_by == "IP":
             # Split stored IPs and check for exact match
             stored_ips = [ip.strip() for ip in vm['ip'].split(',')]
             if search_query in stored_ips:
                 match = True
        
        if match:
            results.append(dict(vm))
    return results

def render_ip_map_page():
    st.title("IP Address Management")
    st.markdown("### Network Availability Map")
    
    col1, col2 = st.columns([3, 1])
    with col1:
        st.info("🟢 Green = Available | 🔴 Red = Taken (In Use)")
    with col2:
        if st.button("🔄 Scan ALL Zones", key="refresh_all_ips"):
            with st.spinner("Scanning ALL configured subnets... This may take a while."):
                data_collector.scan_all_subnets()
            st.success("Bulk scan complete!")
            st.rerun()

    # --- Subnet Management ---
    with st.expander("⚙️ Manage Subnets"):
        m_col1, m_col2 = st.columns([1, 2])
        with m_col1:
            with st.form("add_subnet_form", clear_on_submit=True):
                new_subnet = st.text_input("Add Subnet (e.g., 192.168.50)", help="Enter the first 3 octets")
                if st.form_submit_button("Add"):
                    if new_subnet and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", new_subnet):
                        if db_manager.add_subnet(new_subnet):
                            st.success(f"Added {new_subnet}")
                            st.rerun()
                        else:
                            st.error("Subnet already exists.")
                    else:
                        st.error("Invalid format. Use x.x.x")
        
        with m_col2:
            st.write("Configured Subnets:")
            current_subnets = db_manager.get_all_subnets()
            if current_subnets:
                # specific layout for tags/delete
                for s in current_subnets:
                    c1, c2 = st.columns([4, 1])
                    c1.code(s)
                    if c2.button("🗑️", key=f"del_{s}"):
                        db_manager.remove_subnet(s)
                        st.rerun()
            else:
                st.info("No subnets configured.")

    # --- State Management & URL Sync ---
    available_subnets = db_manager.get_all_subnets()
    if not available_subnets:
        st.warning("No subnets configured. Please add a subnet above.")
        return

    query_params = st.query_params
    qp_subnet = query_params.get("subnet", None)

    # If URL param exists and differs from current state, sync state to URL
    if qp_subnet in available_subnets:
        if st.session_state.get("selected_subnet") != qp_subnet:
            st.session_state.selected_subnet = qp_subnet

    # Initialize default if needed or if selected is invalid
    if "selected_subnet" not in st.session_state or st.session_state.selected_subnet not in available_subnets:
        st.session_state.selected_subnet = available_subnets[0]

    # Render Selectbox linked to session state
    selected_subnet = st.selectbox(
        "Select Zone to View:", 
        available_subnets, 
        index=available_subnets.index(st.session_state.selected_subnet),
        key="subnet_selector"
    )
    
    # Check if subnet changed
    if selected_subnet != st.session_state.selected_subnet:
        st.session_state.selected_subnet = selected_subnet
        if "inspect_ip" in st.query_params:
            del st.query_params["inspect_ip"]
        st.query_params["subnet"] = selected_subnet
        st.rerun()
    
    if st.query_params.get("subnet") != selected_subnet:
        st.query_params["subnet"] = selected_subnet

    # --- Load Data from DB ---
    conn = db_manager.get_db_connection()
    rows = conn.execute("SELECT ip, status FROM network_scans WHERE subnet = ?", (selected_subnet,)).fetchall()
    conn.close()
    
    active_ips = {row['ip'] for row in rows if row['status'] == 'taken'}

    # --- Inspection Logic (Triggered by URL) ---
    inspect_ip = query_params.get("inspect_ip", None)

    if inspect_ip:
        if inspect_ip.startswith(selected_subnet):
            st.divider()
            st.subheader(f"Details for {inspect_ip}")
            
            # DB Search
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
                        st.rerun()
            else:
                if inspect_ip in active_ips:
                    st.warning(f"IP {inspect_ip} is active (pingable) but no VM was found with this IP in the DB.")
                else:
                    st.info(f"IP {inspect_ip} is available (no ping response).")
            
            if st.button("Close Details"):
                if "inspect_ip" in st.query_params:
                    del st.query_params["inspect_ip"]
                st.rerun()
            st.divider()
        else:
            if "inspect_ip" in st.query_params:
                del st.query_params["inspect_ip"]
            st.rerun()

    # --- HTML/CSS Grid Rendering ---
    grid_html = '<div class="ip-grid">'
    for i in range(256):
        current_ip = f"{selected_subnet}.{i}"
        
        if current_ip in active_ips:
            status_class = "ip-taken"
            tooltip = f"{current_ip} (Taken)"
        else:
            status_class = "ip-free"
            tooltip = f"{current_ip} (Available)"
            
        link = f"?page=ip_management&subnet={selected_subnet}&inspect_ip={current_ip}"
        grid_html += f'<a href="{link}" target="_self" class="ip-link"><div class="ip-box {status_class}" title="{tooltip}">{i}</div></a>'
    
    grid_html += '</div>'
    st.markdown(grid_html, unsafe_allow_html=True)

def render_recent_vms_page():
    st.title("🕒 Recently Created VMs")
    
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date", value=datetime.now() - timedelta(days=7))
    with col2:
        end_date = st.date_input("End Date", value=datetime.now())

    if start_date > end_date:
        st.error("Error: End date must fall after start date.")
        return

    # Fetch from DB
    conn = db_manager.get_db_connection()
    # SQLite stores dates as strings usually, need to be careful with comparison
    # Ideally stored as ISO8601 strings YYYY-MM-DD...
    # We will fetch all and filter in python for flexibility with string formats or do exact range query if formats align
    query = """
    SELECT v.*, h.ip as host_ip 
    FROM vms v
    JOIN hosts h ON v.host_id = h.id
    WHERE v.created_date IS NOT NULL
    """
    vms = conn.execute(query).fetchall()
    conn.close()

    found_vms = []
    for vm in vms:
        c_date_str = vm['created_date']
        if not c_date_str: continue
        try:
            # Handle potential 'Z' or offsets
            dt = datetime.fromisoformat(c_date_str.replace('Z', '+00:00'))
            if start_date <= dt.date() <= end_date:
                found_vms.append(dict(vm))
        except ValueError:
            pass
    
    if found_vms:
        st.success(f"Found {len(found_vms)} VMs created in the selected period (Data from DB).")
        display_data = []
        for vm in found_vms:
            state_raw = vm['power_state']
            if "poweredOn" in str(state_raw):
                state_display = "🟢 ↑"
            elif "poweredOff" in str(state_raw):
                state_display = "🔴 ↓"
            else:
                state_display = f"⚪ {state_raw}"

            display_data.append({
                "VM IP": vm['ip'],
                "ESXi Host": vm['host_ip'],
                "Name": vm['name'],
                "Created": vm['created_date'],
                "RAM": vm['ram_info'],
                "CPU": vm['cpu_count'],
                "Storage": vm['disk_info'],
                "State": state_display
            })
        st.dataframe(display_data, use_container_width=True)
    else:
        st.info("No VMs found in DB matching this range.")

# --- UI Rendering Functions ---
def display_host_details(host_ip):
    """Displays the details for a single host from DB."""
    
    col1, col2 = st.columns([5, 1])
    with col1:
        st.header(f"🖥️ Details for {host_ip}")
    with col2:
        st.markdown(f'<a href="https://{host_ip}" target="_blank" class="link-button">View</a>', unsafe_allow_html=True)
        if st.button("← Back to Hub", key=f"back_details_{host_ip}", use_container_width=True):
            st.session_state.host = None
            st.rerun()

    # Get metrics from DB
    hosts = fetch_hosts_with_metrics()
    host_data = next((h for h in hosts if h['ip'] == host_ip), None)

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
        # Convert sqlite rows to dicts for filtering
        vms_list = [dict(vm) for vm in vms]
        if search_query:
            vms_list = [vm for vm in vms_list if search_query.lower() in vm["name"].lower()]
        
        # Rename keys for display to match original
        display_vms = []
        for vm in vms_list:
            state_raw = vm['power_state']
            if "poweredOn" in state_raw:
                state_display = "🟢 ↑"
            elif "poweredOff" in state_raw:
                state_display = "🔴 ↓"
            else:
                state_display = f"⚪ {state_raw}"

            display_vms.append({
                "Name": vm['name'],
                "OS": vm['os'],
                "IP": vm['ip'],
                "CPU (vCPUs)": vm['cpu_count'],
                "RAM": vm['ram_info'],
                "Disks": vm['disk_info'],
                "Created": vm['created_date'],
                "State": state_display
            })
            
        if display_vms: 
            st.dataframe(display_vms, use_container_width=True)
        else: st.info("No VMs found matching the search query.")
    else: st.info("No VMs found on this host in DB.")

def user_management(users_config, username):
    st.title("User Management")

    st.subheader("Add New User")
    with st.form("add_user_form", clear_on_submit=True):
        new_username = st.text_input("Username")
        new_name = st.text_input("Name")
        new_email = st.text_input("Email")
        new_password = st.text_input("Password", type="password")
        new_role = st.selectbox("Role", ["admin", "user"], index=1)
        submitted = st.form_submit_button("Add User")

        if submitted:
            if new_username and new_password:
                # Hash the password
                hashed_password = stauth.Hasher.hash(new_password)
                users_config['credentials']['usernames'][new_username] = {
                    'email': new_email,
                    'name': new_name,
                    'password': hashed_password,
                    'role': new_role
                }
                with open('./users.json', 'w') as file:
                    json.dump(users_config, file, indent=4)
                st.success(f"User {new_username} added successfully!")
            else:
                st.error("Username and Password cannot be empty.")

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
            update_submitted = st.form_submit_button("Update User")

            if update_submitted:
                user_data['name'] = updated_name
                user_data['email'] = updated_email
                user_data['role'] = updated_role
                if new_password_update:
                    user_data['password'] = stauth.Hasher.hash(new_password_update)
                with open('./users.json', 'w') as file:
                    json.dump(users_config, file, indent=4)
                st.success(f"User {selected_username} updated successfully!")

    st.subheader("Delete User")
    with st.form("delete_user_form", clear_on_submit=True):
        user_to_delete = st.selectbox("Select User to Delete", [u for u in current_usernames if u != username])
        delete_submitted = st.form_submit_button("Delete User")

        if delete_submitted:
            if user_to_delete:
                del users_config['credentials']['usernames'][user_to_delete]
                with open('./users.json', 'w') as file:
                    json.dump(users_config, file, indent=4)
                st.success(f"User {user_to_delete} deleted successfully!")
                st.rerun()
            else:
                st.error("Please select a user to delete.")

    if st.button("Back to Dashboard"):
        st.session_state.page = 'dashboard'
        st.rerun()

def main():
    # --- Authentication ---
    with open('./users.json') as file:
        users_config = json.load(file)

    authenticator = stauth.Authenticate(
        users_config['credentials'],
        users_config['cookie']['name'],
        users_config['cookie']['key'],
        users_config['cookie']['expiry_days']
    )

    # Header with Icon and Title
    header_col1, header_col2 = st.columns([1, 10])
    with header_col1:
        try:
            st.image("image.png", width=60)
        except Exception:
            pass
            
    with header_col2:
         st.markdown("<h2 style='margin-top: 10px;'>ESXi Monitoring Dashboard</h2>", unsafe_allow_html=True)


    
    authenticator.login(location='main')

    name = st.session_state.get('name')
    authentication_status = st.session_state.get('authentication_status')
    username = st.session_state.get('username')

    if authentication_status == False:
        st.error('Username/password is incorrect')
        st.stop()
    elif authentication_status == None:
        st.warning('Please enter your username and password')
        st.stop()

    # Get the role from users.json based on the authenticated username
    st.session_state['role'] = users_config['credentials']['usernames'][username]['role']

    # --- Main Application ---
    # Restore navigation via URL params (required for HTML grid links)
    if "page" in st.query_params:
        st.session_state.page = st.query_params["page"]

    if 'page' not in st.session_state:
        st.session_state.page = 'dashboard'
    
    
    with st.sidebar:
        st.title("Menu")
        
        # Theme Toggle
        theme_val = st.toggle("Dark Mode", value=(st.session_state.theme == 'Dark'))
        new_theme = "Dark" if theme_val else "Light"
        if new_theme != st.session_state.theme:
            st.session_state.theme = new_theme
            st.rerun()
            
        st.divider()
        
        if st.button("📊 Dashboard", use_container_width=True):

            st.session_state.page = 'dashboard'
            st.session_state.host = None # Reset host view
            st.session_state.found_vms = None
            st.query_params.clear() 
            st.rerun()
            
        if st.button("🌐 IP Map", use_container_width=True):
            st.session_state.page = 'ip_management'
            st.rerun()

        if st.button("🕒 Recently Created", use_container_width=True):
            st.session_state.page = 'recent_vms'
            st.rerun()

        if st.session_state.get('role') == 'admin':
            if st.button("⚙️ User Mgmt", use_container_width=True):
                st.session_state.page = 'user_management'
                st.rerun()
        
        st.divider()
        authenticator.logout('🚪 Logout', location='sidebar')
        st.divider()
        
        if st.button("🔄 Refresh Data", use_container_width=True):
             with st.spinner("Refreshing..."):
                 data_collector.update_all_hosts()
             st.success("Refreshed!")
             time.sleep(0.5)
             st.rerun()
             



    if st.session_state.page == 'user_management':
        user_management(users_config, username)
    elif st.session_state.page == 'ip_management':
        render_ip_map_page()
    elif st.session_state.page == 'recent_vms':
        render_recent_vms_page()
    else: # Dashboard page

        if 'host' not in st.session_state:
            st.session_state.host = None
        
        if st.session_state.host:
            display_host_details(st.session_state.host)
        else:
            search_by = st.selectbox("Search by:", ["Name", "IP"], key="search_by")
            query = st.text_input(f"Enter VM {search_by} to find its ESXi host:", key="vm_search")

            # Local search in DB
            if query:
                st.session_state.found_vms = fetch_all_vms(query, search_by)
            else:
                st.session_state.found_vms = None

            if st.session_state.found_vms:
                st.success(f"Found {len(st.session_state.found_vms)} VMs matching your query:")
                for i, vm in enumerate(st.session_state.found_vms):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.write(f"**VM Name:** {vm['name']} | **VM IP:** {vm['ip']} | **ESXi Host:** {vm['host_ip']}")
                    with col2:
                        if st.button("View Host", key=f"view_host_{i}_{vm['name']}"):
                            st.session_state.host = vm['host_ip']
                            st.session_state.found_vms = None
                            st.rerun()
            elif query and not st.session_state.found_vms:
                 st.error(f"No VMs found matching '{query}'.")

            st.header("ESXi Host Overview")
            
            sort_col1, sort_col2 = st.columns(2)
            sort_by = sort_col1.selectbox("Sort by:", ["Default", "CPU", "Memory", "Storage"], key="host_sort_by")
            with sort_col2:
                st.markdown("<div style='height: 29px;'></div>", unsafe_allow_html=True)
                sort_desc = st.checkbox("Descending", key="host_sort_desc")

            # Fetch all host metrics from DB
            all_hosts_with_metrics = fetch_hosts_with_metrics()
            
            # Sort
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
                        
                        if host_data['cpu_usage'] is None:
                            st.warning("No data available.")
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
                            if st.button("Details", key=f"btn_details_{host_data['ip']}"):
                                st.session_state.host = host_data['ip']
                                st.rerun()
                        with b_col2:
                            st.markdown(f'<a href="https://{host_data["ip"]}" target="_blank" class="link-button">View</a>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()