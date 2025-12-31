# ESXi Monitoring Dashboard

A lightweight, real-time monitoring and IP management solution for VMware ESXi environments. Built with **Streamlit**, **SQLite**, and **pyVmomi**.

## 🚀 Features

- **Host Overview**: Visualized resource usage (CPU, RAM, Storage) across multiple ESXi hosts.
- **VM Inventory**: Track Virtual Machines, their guest operating systems, IP addresses, hardware configurations, and power states.
- **IP Management (IPAM)**: An interactive visual heatmap of network subnets showing live (pingable) vs. available IP addresses.
- **Recent VM Tracking**: Quickly identify VMs created within a specific date range.
- **Role-Based Access Control**: Secure authentication for 'Admin' and 'User' roles.

## 🛠️ How It Works

1.  **Data Collection**: The `data_collector.py` module uses the `pyVmomi` library to interface with VMware's vSphere API. It retrieves hardware metrics and VM snapshots from configured hosts.
2.  **Network Scanning**: The application performs multi-threaded ICMP (ping) scans across defined subnets to populate the IP Management grid.
3.  **Persistence**: Data is stored in a local SQLite database (`monitoring.db`). This ensures the dashboard remains fast and responsive by serving cached data, which is periodically updated.
4.  **Frontend**: The UI is built using Streamlit, featuring a modern theme inspired by the IBM Carbon Design System.

## 📦 Installation

### 1. Prerequisites
- Python 3.9+
- Network access to ESXi hosts (Port 443).

### 2. Setup
Clone the repository and install the required dependencies:

```bash
pip install -r requirements.txt
```

### 3. Configuration
Create a `.env` file in the root directory to store your ESXi host passwords securely:

```env
GROUP1_PASS=your_password_here
GROUP2_PASS=your_password_here
```

*Note: Host IPs and groups are currently configured in the seeding logic within `monitoring_dashboard.py`.*

## 🚦 Usage

### Start the Dashboard
Run the Streamlit application to access the web interface:

```bash
streamlit run monitoring_dashboard.py
```

### Start the Background Updater
To keep the data fresh without keeping the dashboard open, run the background worker:

```bash
python background_job.py
```

## 🔒 Security
- Sensitive files like `.env`, `monitoring.db`, and `users.json` are excluded from version control via `.gitignore`.
- Password hashing is used for dashboard user accounts via `streamlit-authenticator`.
