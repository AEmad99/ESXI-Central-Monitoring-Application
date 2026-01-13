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
1.  **Environment Variables**: Create a `.env` file in the root directory to store your ESXi host passwords:
    ```env
    GROUP1_PASS=your_password_here
    GROUP2_PASS=your_password_here
    ```
2.  **User Config**: The application requires a `users.json` file for authentication.
    - **First Run**: If this file is missing, create a generic one manually or use the snippet below.
    
    <details>
    <summary><b>Click to copy default <code>users.json</code> content</b></summary>

    ```json
    {
        "cookie": {
            "expiry_days": 30,
            "key": "random_signature_key",
            "name": "auth_cookie"
        },
        "credentials": {
            "usernames": {
                "admin": {
                    "name": "Admin",
                    "password": "$2b$12$vFNrfXSy86Xn1khBV6QJHOzhxmFzCCNTops./G1F/csKcRFOq7vg6",
                    "role": "admin"
                }
            }
        }
    }
    ```
    </details>
    ```
    </details>
    *The default password for the above config is `admin`.*

    #### Session Expiration Configuration
    To configure how long a user stays logged in, modify the `expiry_days` value in `users.json`.
    - **8 Hours**: `0.333333` (8/24)
    - **1 Day**: `1`
    - **30 Days**: `30`
    - **1 Minute (Testing)**: `0.000694`

## 🚦 Usage

### Start the Application
Run the Streamlit application to launch the dashboard:

```bash
streamlit run monitoring_dashboard.py
```

- **Default credentials**: `admin` / `admin`
- Access the dashboard at `http://localhost:8501`

### Background Data Collection
To keep data fresh, run the background worker in a separate terminal:

```bash
python background_job.py
```

## 🔒 Security
- Sensitive files (`.env`, `monitoring.db`, `users.json`, logos) are excluded from version control via `.gitignore`.
- Password hashing is used for dashboard user accounts via `streamlit-authenticator`.

