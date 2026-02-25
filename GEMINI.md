# Project Context: ESXi Monitoring Dashboard

## 1. Project Overview
This project is a local-first, lightweight ESXi Monitoring Dashboard. It provides real-time monitoring of VMware ESXi hosts, IP address management (IPAM), and features an integrated AI assistant.

**Primary Goal:** Monitor ESXi hosts/VMs and manage network IP usage via a unified web interface.

## 2. Tech Stack
- **Language:** Python 3.x
- **Frontend:** Streamlit (with `streamlit-authenticator` for auth, custom CSS for theming).
- **Backend/Database:** SQLite (WAL mode enabled) with SQLAlchemy ORM.
- **Virtualization API:** `pyVmomi` (VMware vSphere API).
- **AI/LLM:** Google Gemini API (`google.generativeai`).
- **Network Scanning:** Python `subprocess` (ping) and `socket` (port checking).

## 3. System Architecture & Data Flow
The application consists of four distinct components that interact as follows:

### A. Frontend & UI (`monitoring_dashboard.py`)
- **Role:** Main entry point. Orchestrates views: Dashboard, IP Map, History, Recent VMs, AI Agent, User Management.
- **State Management:** Reads the *latest* cached state from the SQLite DB (does not poll API directly during render).
- **Auth:** Uses local `users.json` for session/role management.

### B. Database Layer (`database.py`, `models.py`)
- **File:** `monitoring.db` (SQLite).
- **Concurrency:** MUST use Write-Ahead Logging (WAL) mode to handle concurrent reads (UI) and writes (Background Job).
- **Key Models:**
  - `ESXiHost` & `HostMetrics`: Physical server stats (CPU/RAM/Storage).
  - `VM`: Virtual Machine details (OS, IP, Power State).
  - `IPLease`, `NetworkDevice`, `HistoryLog`: IPAM tracking (Active/Reserved/Free).

### C. Data Collection Engine (`data_collector.py`, `background_job.py`)
- **Mechanism:** Runs asynchronously in the background.
- **vSphere:** Uses `pyVmomi` to pull hardware summaries and VM inventory.
- **Scanning:** Uses `ping` (ICMP) with smart fallback to ports 53, 3389, 445, 22 if ICMP is blocked.
- **Data Flow:** `Collector` -> `Update SQLite DB`. The UI *never* calls the collector directly; it only reads the DB.

### D. AI Infrastructure Agent (`ai_agent.py`)
- **Model:** Google Gemini Pro.
- **Capabilities:**
  1. **Text-to-SQL:** Converts natural language to **read-only** SQL queries against `monitoring.db`.
  2. **Deployment Recommendations:** Calculates capacity on `ESXiHost` models to recommend placement for new VMs.
  3. **Analysis:** Summarizes cluster utilization.

## 4. Coding Guidelines & Constraints
1.  **Database Locking:** Always ensure database sessions are scoped correctly to avoid locking `monitoring.db`. Use context managers for DB sessions.
2.  **Streamlit Performance:** Use `@st.cache_data` or `@st.cache_resource` where appropriate, but ensure data freshness isn't stale for monitoring.
3.  **Safety:** The AI Agent is strictly **Read-Only** on the database side. It should never generate SQL `INSERT`, `UPDATE`, or `DELETE` commands.
4.  **UI/UX:** Maintain the distinction between "Light" and "Dark" modes in CSS injections.
5.  **Async/Sync:** The UI is synchronous (Streamlit standard), but background jobs must remain non-blocking.

## 5. File Structure Map
- `monitoring_dashboard.py`: Main UI application.
- `database.py`: DB connection string and session handling.
- `models.py`: SQLAlchemy table definitions.
- `data_collector.py`: Logic for `pyVmomi` and network scanning.
- `background_job.py`: Scheduler/Runner for the collector.
- `ai_agent.py`: Gemini API integration and prompt logic.
- `users.json`: User credentials (hashed) and configuration.
