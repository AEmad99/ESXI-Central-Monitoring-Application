import sqlite3
import requests
import json
import re
import os
import pandas as pd
import streamlit as st
import google.generativeai as genai
from datetime import datetime

# =============================================================================
# Configuration
# =============================================================================

DB_PATH = "monitoring.db"
MAX_CHAT_HISTORY = 10

# --- Gemini API ---
GEMINI_MODEL = "gemini-2.5-flash"

# =============================================================================
# System Prompts
# =============================================================================

SYSTEM_PROMPT_SQL = """You are a SQLite query generator for an ESXi virtualization monitoring system.
Generate ONLY the SQL query to answer the user's question. Wrap it in [SQL] and [/SQL] tags.

ENVIRONMENT:
- 16 ESXi hosts in 2 groups.
- Group 1 (4 hosts): 192.168.1.2, 192.168.1.7, 192.168.1.8, 192.168.1.9
- Group 2 (12 hosts): 192.168.1.3, 192.168.1.4, 192.168.1.5, 192.168.1.6, 192.168.1.13, 192.168.1.14, 192.168.1.15, 192.168.1.16, 192.168.0.170, 192.168.0.180, 192.168.0.190, 192.168.0.200
- 15 subnets: 192.168.0 through 192.168.14

SCHEMA:
1. esxi_hosts: id(PK), ip(unique), username, password, group_name
   - group_name values: 'Group 1', 'Group 2'

2. host_metrics: id(PK), host_id(FK->esxi_hosts.id), cpu_usage(%), used_cpu_ghz, total_cpu_ghz, mem_usage(%), used_mem_gb, total_mem_gb, storage_usage(%), used_storage_gb, total_storage_gb, last_updated
   - HOST-LEVEL metrics ONLY. One row per physical host.

3. vms: id(PK), host_id(FK->esxi_hosts.id), name, os, ip, cpu_count(int), cpu_usage_mhz(int), cpu_total_mhz(int), cpu_usage(float %), ram_used_mb(int), ram_total_mb(int), ram_usage(float %), ram_info(display text), disk_total_gb(float), disk_info(display text), created_date, power_state, last_updated
   - power_state: 'poweredOn', 'poweredOff', 'suspended'
   - VM resource columns (all numeric, directly queryable):
     * cpu_count: number of vCPUs allocated
     * cpu_usage_mhz: current CPU usage in MHz
     * cpu_total_mhz: total allocated CPU capacity in MHz
     * cpu_usage: CPU utilization percentage (0-100)
     * ram_used_mb: active/used guest RAM in MB
     * ram_total_mb: total allocated RAM in MB
     * ram_usage: RAM utilization percentage (0-100)
     * disk_total_gb: sum of all virtual disk capacities in GB
   - Display-only text columns (do NOT parse these for queries):
     * ram_info: text like "368 / 12288 MB (3.0%)" — for display only
     * disk_info: text like "Hard disk 1 (100.0GB)" — for display only
   - os values include: 'Red Hat Enterprise Linux 7/8/9 (64-bit)', 'Microsoft Windows Server 2016/2019 (64-bit)', 'CentOS 7/8 (64-bit)', 'CoreOS Linux (64-bit)', 'Ubuntu Linux (64-bit)', 'Other (64-bit)', etc.
   - VM name patterns: project prefixes like CP4D, MAS, MASHOST, MASHigh, CP4DWA, CP4I, KW, NEWMAS, TRIRIGA, NOI, Instana, BAW, METAL LB, VIRT, etc.
   - ip can be 'N/A' if VM is off/unreachable. VMs may have multiple IPs comma-separated.

4. network_devices: id(PK), mac_address(unique), hostname, first_seen, last_seen, type

5. ip_leases: ip(PK), subnet, status, device_id(FK->network_devices.id), last_updated
   - status values: 'FREE', 'ACTIVE', 'RESERVED' (stored uppercase).

6. history_log: id(PK), timestamp, ip, status, device_id(FK->network_devices.id), hostname_snapshot

7. subnets: prefix(PK) - values: '192.168.0' through '192.168.14'

NAME MATCHING:
- Use LIKE '%term%' for fuzzy match. Case-insensitive: use LIKE or LOWER().

JOINS:
- vms.host_id = esxi_hosts.id
- host_metrics.host_id = esxi_hosts.id
- ip_leases.device_id = network_devices.id
- Alias when both tables have ip: vms.ip AS vm_ip, esxi_hosts.ip AS host_ip

DISAMBIGUATION:
- "machine"/"VM"/"guest" resource usage → vms table. Use ram_usage, cpu_usage, disk_total_gb, ram_total_mb, cpu_count etc.
- "host"/"server"/"hypervisor" resource usage → host_metrics table (cpu_usage, mem_usage, storage_usage).
- "most utilized VMs" → ORDER BY ram_usage DESC or cpu_usage DESC from vms table.
- "VMs with most RAM" → ORDER BY ram_total_mb DESC from vms table.
- NEVER join host_metrics to answer questions about VM/machine utilization. host_metrics is for hosts only.

RULES:
- NEVER select the password column from esxi_hosts.
- Output ONLY: [SQL]query[/SQL]. No explanation."""

SYSTEM_PROMPT_CHAT = """You are the AI Infrastructure Agent for an ESXi Monitoring Dashboard.
You manage 16 ESXi hosts in 2 groups across 15 subnets (192.168.0-14).

Your capabilities:
- Query infrastructure data (VMs, hosts, IPs, metrics)
- Recommend where to deploy new VMs based on available resources
- Analyze infrastructure health and utilization patterns

Keep responses brief, friendly, and helpful. Match response length to question complexity."""

# =============================================================================
# Core LLM Layer
# =============================================================================

def _get_gemini_model(system=None):
    """Build Gemini model. Prioritizes session_state key, falls back to .env."""
    # Check session state first (BYOK)
    api_key = st.session_state.get("gemini_api_key")
    
    # Fallback to .env
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY")
        
    if not api_key:
        raise ValueError("Gemini API Key not provided. Enter it in the sidebar.")
        
    genai.configure(api_key=api_key, transport='rest')
    return genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=system
    )


def call_llm(prompt, options=None, system=None):
    """Non-streaming Gemini call. Returns full text response."""
    try:
        model = _get_gemini_model(system=system)
        response = model.generate_content(prompt)
        return response.text.strip()
    except ValueError as e:
        return f"[Error: {e}]"
    except Exception as e:
        return f"[Error: {str(e)}]"


def call_llm_streaming(prompt, options=None, system=None):
    """Streaming Gemini call. Yields tokens."""
    try:
        model = _get_gemini_model(system=system)
        response = model.generate_content(prompt, stream=True)
        for chunk in response:
            if chunk.text:
                yield chunk.text
    except ValueError as e:
        yield f"[Error: {e}]"
    except Exception as e:
        yield f"[Error: {str(e)}]"


def extract_sql(llm_response):
    """Extract SQL from LLM response using multiple parsing strategies."""
    # Priority 1: [SQL] tags
    match = re.search(r"\[SQL\](.*?)\[/SQL\]", llm_response, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Priority 2: Markdown code blocks
    match = re.search(r"```(?:sql)?\n?(.*?)\n?```", llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Priority 3: Line starting with SELECT
    for line in llm_response.split('\n'):
        stripped = line.strip()
        if stripped.upper().startswith("SELECT"):
            return stripped

    return None


def execute_sql(query):
    """Execute SQL with security checks. Returns DataFrame or error string."""
    # Clean LLM artifacts
    query = re.sub(r"```sql\n?|```\n?", "", query, flags=re.IGNORECASE).strip()
    if query.upper().startswith("SQL"):
        query = query[3:].strip()

    # Security: block destructive operations
    first_word = query.strip().split()[0].upper() if query.strip() else ""
    if first_word in ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE", "TRUNCATE"):
        return "Only SELECT queries are allowed."

    # Security: block password access
    if "password" in query.lower() and "select" in query.lower():
        return "Cannot query the password column."

    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(query, conn)
        conn.close()

        # Deduplicate column names
        cols = pd.Series(df.columns)
        for dup in cols[cols.duplicated()].unique():
            indices = cols[cols == dup].index.tolist()
            cols[indices] = [dup + '_' + str(i) if i != 0 else dup for i in range(len(indices))]
        df.columns = cols

        return df
    except Exception as e:
        return f"SQL Error: {str(e)}"

# =============================================================================
# Chat History
# =============================================================================

def init_chat_history():
    if "agent_chat_history" not in st.session_state:
        st.session_state.agent_chat_history = []


def add_to_history(role, content):
    st.session_state.agent_chat_history.append({
        "role": role,
        "content": content,
    })
    # Cap history length
    max_entries = MAX_CHAT_HISTORY * 2
    if len(st.session_state.agent_chat_history) > max_entries:
        st.session_state.agent_chat_history = st.session_state.agent_chat_history[-max_entries:]


def render_chat_history():
    for msg in st.session_state.agent_chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# =============================================================================
# Intent Classifier (Pure Python — no LLM call)
# =============================================================================

def classify_intent(user_input):
    """Classify user intent via regex. Returns: deploy_recommend, infra_analysis, general_chat, or sql_query."""
    text = user_input.lower().strip()

    deploy_patterns = [
        r'\bdeploy\b', r'\bprovision\b', r'\bplace\b',
        r'\bwhere\s+(?:should|can|to)\b.*\b(?:deploy|put|run|host|place)\b',
        r'\b(?:need|want|require)\s+\d+\s*(?:vcpu|cpu|core|gb|ram|mem|storage|disk)\b',
        r'\bfit\b.*\b(?:vm|machine|server|workload)\b',
        r'\brecommend\b.*\b(?:host|server|deploy)\b',
        r'\bavailable\s+(?:resources|capacity)\b.*\bfor\b',
        r'\bwhich\s+host\b.*\b(?:has|enough|available|capacity)\b',
        r'\bcreate\s+(?:a\s+)?(?:new\s+)?(?:vm|machine)\b',
        r'\bdistribute\b.*\b(?:load|vm|machine|workload)\b',
        r'\bcluster\b.*\bdeploy\b',
    ]

    analysis_patterns = [
        r'\b(?:overall|general)\s+(?:health|status|state)\b',
        r'\b(?:infrastructure|cluster|environment)\s+(?:summary|overview|analysis|report|health)\b',
        r'\bhow\s+(?:is|are)\s+(?:my|the|our)\s+(?:infrastructure|environment|cluster)\b',
        r'\bcapacity\s+(?:planning|report|summary)\b',
        r'\b(?:full|complete|overall)\s+(?:overview|summary|report)\b',
    ]

    chat_patterns = [
        r'^(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|greetings|salam|yo)\b',
        r'^(?:thanks|thank\s+you|thx|ty)\b',
        r'\bwhat\s+can\s+you\s+do\b', r'^help$',
        r'^(?:who|what)\s+are\s+you\b',
    ]

    for p in deploy_patterns:
        if re.search(p, text):
            return "deploy_recommend"
    for p in analysis_patterns:
        if re.search(p, text):
            return "infra_analysis"
    for p in chat_patterns:
        if re.search(p, text):
            return "general_chat"

    return "sql_query"

# =============================================================================
# Skill 1: SQL Query Pipeline
# =============================================================================

@st.cache_data(ttl=120, show_spinner=False)
def _resolve_context_hints(user_input):
    """Extract approximate terms from user input and resolve them to actual DB values.
    Returns a short context string to append to the SQL prompt.
    Cached for 120s — DB only changes on collection cycles."""
    text = user_input.lower()
    hints = []

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        # Extract potential name references (words 3+ chars, not common English/query words)
    skip_words = {'show', 'list', 'find', 'give', 'tell', 'what', 'which', 'where', 'that',
                  'have', 'more', 'than', 'with', 'from', 'this', 'them', 'their', 'most',
                  'least', 'each', 'every', 'many', 'much', 'host', 'hosts', 'machine',
                  'machines', 'the', 'all', 'are', 'how', 'does', 'usage', 'using',
                  'running', 'powered', 'turned', 'storage', 'memory', 'disk', 'off', 'per',
                  'ram', 'cpu', 'vcpu', 'vcpus', 'utilization', 'group', 'subnet', 'for',
                  'and', 'but', 'not', 'any', 'can', 'vms', 'ips', 'total', 'count',
                  'server', 'servers', 'cluster', 'node', 'nodes', 'project', 'projects',
                  'about', 'info', 'information', 'details', 'detail', 'get', 'see'}
    words = re.findall(r'[a-zA-Z0-9_.-]{3,}', user_input)
    search_terms = [w for w in words if w.lower() not in skip_words]

    # Try to match VM names
    for term in search_terms:
        cur.execute("SELECT DISTINCT name FROM vms WHERE LOWER(name) LIKE ? LIMIT 8",
                    (f'%{term.lower()}%',))
        matches = [r[0] for r in cur.fetchall()]
        if matches:
            hints.append(f"VM names matching '{term}': {', '.join(matches)}")

    # Try to match host IPs
    for term in search_terms:
        if re.match(r'^\d', term):
            cur.execute("SELECT ip FROM esxi_hosts WHERE ip LIKE ? LIMIT 5",
                        (f'%{term}%',))
            matches = [r[0] for r in cur.fetchall()]
            if matches:
                hints.append(f"Host IPs matching '{term}': {', '.join(matches)}")

    # Try to match group names
    for term in search_terms:
        cur.execute("SELECT DISTINCT group_name FROM esxi_hosts WHERE LOWER(group_name) LIKE ?",
                    (f'%{term.lower()}%',))
        matches = [r[0] for r in cur.fetchall()]
        if matches:
            hints.append(f"Groups matching '{term}': {', '.join(matches)}")

    # Try to match OS names
            for term in search_terms:
                cur.execute("SELECT DISTINCT os FROM vms WHERE LOWER(os) LIKE ? LIMIT 5",
                            (f'%{term.lower()}%',))
                matches = [r[0] for r in cur.fetchall()]
                if matches:
                    hints.append(f"OS matching '{term}': {', '.join(matches)}")
    
        if hints:        return "\n\nRESOLVED VALUES:\n" + "\n".join(hints)
    return ""


def handle_sql_query(user_input, chat_history):
    """Generate SQL → execute → show results → stream NL summary."""
    context_hints = _resolve_context_hints(user_input)
    user_prompt = context_hints + f"\n\nQuestion: {user_input}\nSQL:" if context_hints else f"Question: {user_input}\nSQL:"

    with st.spinner("Generating query..."):
        raw_response = call_llm(user_prompt, system=SYSTEM_PROMPT_SQL)

    sql = extract_sql(raw_response)

    if not sql:
        st.warning("I couldn't generate a valid SQL query for that. Could you try rephrasing?")
        return "[Could not generate SQL]"

    with st.expander("Generated SQL", expanded=False):
        st.code(sql, language="sql")

    result = execute_sql(sql)

    if isinstance(result, str):
        st.error(result)
        return f"[SQL Error: {result}]"

    if result.empty:
        st.info("Query ran successfully but returned no results.")
        return "[Query returned no results]"

    st.dataframe(result, width='stretch')

    # Stream a natural language summary for manageable result sets
    if len(result) <= 50:
        summary_data = result.to_string(index=False, max_rows=20)
        col_names = ", ".join(result.columns.tolist())
        summary_prompt = f"""The user asked: "{user_input}"
The query returned {len(result)} rows with columns: {col_names}
Data:
{summary_data}

Summarize the data in 1-3 sentences. Mention specific names and numbers. Do NOT invent information."""

        st.markdown("---")
        placeholder = st.empty()
        full_text = ""
        for token in call_llm_streaming(summary_prompt, system=SYSTEM_PROMPT_CHAT):
            full_text += token
            placeholder.markdown(full_text)
        return full_text
    else:
        st.success(f"Retrieved {len(result)} records.")
        return f"[Retrieved {len(result)} records]"

# =============================================================================
# Skill 2: General Chat
# =============================================================================

def handle_general_chat(user_input, chat_history):
    """Conversational responses for greetings, help, etc."""
    history_context = ""
    if chat_history:
        recent = chat_history[-4:]
        for msg in recent:
            content = msg["content"][:200]
            history_context += f"{msg['role']}: {content}\n"

    prompt = f"""{f'Recent conversation:{chr(10)}{history_context}{chr(10)}' if history_context else ''}User: {user_input}
Assistant:"""

    placeholder = st.empty()
    full_text = ""
    for token in call_llm_streaming(prompt, system=SYSTEM_PROMPT_CHAT):
        full_text += token
        placeholder.markdown(full_text)
    return full_text

# =============================================================================
# Skill 3: Infrastructure Analysis
# =============================================================================

def _detect_analysis_focus(user_input):
    """Detect what the user's analysis question focuses on. Returns dict of focus areas."""
    text = user_input.lower()
    focus = {
        "wants_filter": False,
        "filter_resources": [],  # 'ram', 'cpu', 'storage'
        "filter_threshold": None,
        "wants_overloaded": False,
        "wants_underutilized": False,
    }

    # Detect explicit threshold: "more than 75%", "> 80%", "above 60%", "over 90%", "exceeding 60%"
    threshold_match = re.search(r'(?:more\s+than|above|over|exceed\w*|>\s*|greater\s+than|higher\s+than)\s*(\d+)\s*%', text)
    if threshold_match:
        focus["wants_filter"] = True
        focus["filter_threshold"] = int(threshold_match.group(1))

    # Detect which resources they care about
    if re.search(r'\b(?:ram|memory|mem)\b', text):
        focus["filter_resources"].append("ram")
    if re.search(r'\b(?:cpu|processor|core)\b', text):
        focus["filter_resources"].append("cpu")
    if re.search(r'\b(?:storage|disk|datastore)\b', text):
        focus["filter_resources"].append("storage")

    if re.search(r'\b(?:overload\w*|high\s+usage|stressed|critical|maxed|bottleneck)\b', text):
        focus["wants_overloaded"] = True
    if re.search(r'\b(?:underutiliz\w*|idle|low\s+usage|wasted|unused)\b', text):
        focus["wants_underutilized"] = True

    return focus


def handle_infra_analysis(user_input):
    """Pre-compute infrastructure stats, show tables only when relevant, stream LLM analysis."""
    conn = sqlite3.connect(DB_PATH)

    host_stats = pd.read_sql_query("""
        SELECT e.ip, e.group_name,
               m.cpu_usage, m.mem_usage, m.storage_usage,
               m.used_cpu_ghz, m.total_cpu_ghz,
               m.used_mem_gb, m.total_mem_gb,
               m.used_storage_gb, m.total_storage_gb
        FROM esxi_hosts e
        LEFT JOIN host_metrics m ON e.id = m.host_id
        ORDER BY e.group_name, e.ip
    """, conn)

    vm_stats = pd.read_sql_query("""
        SELECT e.ip as host_ip,
               COUNT(v.id) as total_vms,
               SUM(CASE WHEN v.power_state='poweredOn' THEN 1 ELSE 0 END) as on_vms,
               COALESCE(SUM(v.cpu_count), 0) as total_vcpus
        FROM esxi_hosts e
        LEFT JOIN vms v ON e.id = v.host_id
        GROUP BY e.id
    """, conn)

    conn.close()

    if host_stats.empty:
        st.warning("No host data available. Run a data collection cycle first.")
        return "[No host data available]"

    focus = _detect_analysis_focus(user_input)

    total_hosts = len(host_stats)
    avg_cpu = host_stats['cpu_usage'].mean() or 0
    avg_mem = host_stats['mem_usage'].mean() or 0
    avg_storage = host_stats['storage_usage'].mean() or 0
    total_vms = vm_stats['total_vms'].sum()
    total_on = vm_stats['on_vms'].sum()

    # --- Decide whether to show a filtered table ---
    shown_table = False
    table_description = ""

    if focus["wants_filter"] and focus["filter_threshold"] is not None:
        threshold = focus["filter_threshold"]
        resources = focus["filter_resources"]

        # Build filter condition based on requested resources
        conditions = []
        col_map = {"ram": "mem_usage", "cpu": "cpu_usage", "storage": "storage_usage"}
        if not resources:
            # No specific resource mentioned with threshold — apply to all
            resources = ["ram", "cpu", "storage"]

        for r in resources:
            col = col_map.get(r)
            if col:
                conditions.append(host_stats[col] > threshold)

        if conditions:
            combined = conditions[0]
            for c in conditions[1:]:
                combined = combined | c
            filtered = host_stats[combined].copy()

            if not filtered.empty:
                display_cols = ['ip', 'group_name']
                rename = {'ip': 'Host IP', 'group_name': 'Group'}
                for r in resources:
                    col = col_map[r]
                    display_cols.append(col)
                    rename[col] = {'cpu_usage': 'CPU %', 'mem_usage': 'Memory %', 'storage_usage': 'Storage %'}[col]

                display_df = filtered[display_cols].copy()
                display_df.rename(columns=rename, inplace=True)
                display_df = display_df.sort_values(by=list(rename.values())[2:], ascending=False)

                resource_label = ', '.join(r.upper() for r in resources)
                st.markdown(f"### Hosts with {resource_label} > {threshold}%")
                st.dataframe(display_df, width='stretch', hide_index=True)
                shown_table = True
                table_description = f"Showed table: {len(filtered)} hosts with {resource_label} > {threshold}%."

    elif focus["wants_overloaded"]:
        overloaded = host_stats[
            (host_stats['cpu_usage'] > 80) | (host_stats['mem_usage'] > 80) | (host_stats['storage_usage'] > 85)
        ]
        if not overloaded.empty:
            display_df = overloaded[['ip', 'group_name', 'cpu_usage', 'mem_usage', 'storage_usage']].copy()
            display_df.columns = ['Host IP', 'Group', 'CPU %', 'Memory %', 'Storage %']
            st.markdown("### Overloaded Hosts")
            st.dataframe(display_df, width='stretch', hide_index=True)
            shown_table = True
            table_description = f"Showed table: {len(overloaded)} overloaded hosts."

    elif focus["wants_underutilized"]:
        underutilized = host_stats[
            (host_stats['cpu_usage'] < 20) & (host_stats['mem_usage'] < 30)
        ]
        if not underutilized.empty:
            display_df = underutilized[['ip', 'group_name', 'cpu_usage', 'mem_usage', 'storage_usage']].copy()
            display_df.columns = ['Host IP', 'Group', 'CPU %', 'Memory %', 'Storage %']
            st.markdown("### Underutilized Hosts")
            st.dataframe(display_df, width='stretch', hide_index=True)
            shown_table = True
            table_description = f"Showed table: {len(underutilized)} underutilized hosts."

    # --- Build LLM context ---
    overloaded_all = host_stats[
        (host_stats['cpu_usage'] > 80) | (host_stats['mem_usage'] > 80) | (host_stats['storage_usage'] > 85)
    ]
    underutilized_all = host_stats[
        (host_stats['cpu_usage'] < 20) & (host_stats['mem_usage'] < 30)
    ]

    host_lines = []
    for _, row in host_stats.iterrows():
        vm_row = vm_stats[vm_stats['host_ip'] == row['ip']]
        vms = int(vm_row.iloc[0]['total_vms']) if not vm_row.empty else 0
        on = int(vm_row.iloc[0]['on_vms']) if not vm_row.empty else 0
        host_lines.append(
            f"  {row['ip']} ({row['group_name']}): CPU {row['cpu_usage']:.1f}%, "
            f"Mem {row['mem_usage']:.1f}%, Storage {row['storage_usage']:.1f}%, "
            f"{on}/{vms} VMs on"
        )

    context = f"""Infrastructure Data:
- {total_hosts} ESXi hosts, groups: {', '.join(host_stats['group_name'].dropna().unique())}
- {total_vms} total VMs ({total_on} powered on)
- Average utilization: CPU {avg_cpu:.1f}%, Memory {avg_mem:.1f}%, Storage {avg_storage:.1f}%
- Overloaded hosts (CPU>80% or Mem>80% or Storage>85%): {', '.join(overloaded_all['ip'].tolist()) if not overloaded_all.empty else 'None'}
- Underutilized hosts (CPU<20% and Mem<30%): {', '.join(underutilized_all['ip'].tolist()) if not underutilized_all.empty else 'None'}

Per-host breakdown:
{chr(10).join(host_lines)}"""

    # --- Prompt: scale response length to question complexity ---
    if shown_table:
        length_guide = "A table was already shown above. Write a concise summary (3-5 sentences) that interprets the table data, highlights the most critical hosts, and gives one actionable recommendation. Do not repeat all the numbers from the table."
    else:
        length_guide = "No table was shown. Provide a complete analysis (4-8 sentences). Be specific with host IPs, percentages, and VM counts. Highlight concerns, positives, and give actionable recommendations."

    analysis_prompt = f"""The user asked: "{user_input}"

{context}

{length_guide}
Be friendly and conversational. Reference specific host IPs and numbers. End with a clear recommendation."""

    if shown_table:
        st.markdown("---")
    placeholder = st.empty()
    full_text = ""
    for token in call_llm_streaming(analysis_prompt, system=SYSTEM_PROMPT_CHAT):
        full_text += token
        placeholder.markdown(full_text)
    return full_text

# =============================================================================
# Skill 4: Deployment Recommendations
# =============================================================================

def parse_deployment_requirements(user_input):
    """Extract resource requirements from natural language. Returns dict or None."""
    text = user_input.lower()
    reqs = {"vcpus": 0, "ram_gb": 0, "storage_gb": 0, "count": 1}

    # vCPUs
    cpu_match = re.search(r'(\d+)\s*(?:vcpu|cpu|core)s?', text)
    if cpu_match:
        reqs["vcpus"] = int(cpu_match.group(1))

    # RAM in GB
    ram_match = re.search(r'(\d+)\s*(?:gb|gig)(?:\s*(?:of\s+)?(?:ram|mem(?:ory)?))', text)
    if ram_match:
        reqs["ram_gb"] = int(ram_match.group(1))

    # RAM in MB
    if reqs["ram_gb"] == 0:
        ram_mb = re.search(r'(\d+)\s*mb\s*(?:of\s+)?(?:ram|mem)', text)
        if ram_mb:
            reqs["ram_gb"] = round(int(ram_mb.group(1)) / 1024, 1)

    # Storage
    storage_match = re.search(r'(\d+)\s*(?:gb|gig)(?:\s*(?:of\s+)?(?:storage|disk|ssd|hdd))', text)
    if storage_match:
        reqs["storage_gb"] = int(storage_match.group(1))

    # Count
    count_match = re.search(r'(\d+)\s*(?:vm|machine|instance|server|node)s?', text)
    if count_match:
        reqs["count"] = int(count_match.group(1))

    if reqs["vcpus"] == 0 and reqs["ram_gb"] == 0 and reqs["storage_gb"] == 0:
        return None

    return reqs


def get_host_availability():
    """Compute available resources per host."""
    conn = sqlite3.connect(DB_PATH)

    hosts_df = pd.read_sql_query("""
        SELECT e.id, e.ip, e.group_name,
               m.total_cpu_ghz, m.used_cpu_ghz, m.cpu_usage,
               m.total_mem_gb, m.used_mem_gb, m.mem_usage,
               m.total_storage_gb, m.used_storage_gb, m.storage_usage
        FROM esxi_hosts e
        LEFT JOIN host_metrics m ON e.id = m.host_id
    """, conn)

    vm_alloc_df = pd.read_sql_query("""
        SELECT host_id,
               COUNT(*) as vm_count,
               SUM(CASE WHEN power_state = 'poweredOn' THEN 1 ELSE 0 END) as powered_on,
               COALESCE(SUM(cpu_count), 0) as total_vcpus_allocated,
               COALESCE(SUM(ram_total_mb), 0) as total_ram_allocated_mb,
               COALESCE(SUM(disk_total_gb), 0) as total_disk_allocated_gb
        FROM vms
        GROUP BY host_id
    """, conn)

    conn.close()

    availability = []
    for _, host in hosts_df.iterrows():
        host_vms = vm_alloc_df[vm_alloc_df['host_id'] == host['id']]

        vm_count = 0
        powered_on = 0
        vcpus_allocated = 0

        if not host_vms.empty:
            row = host_vms.iloc[0]
            vm_count = int(row['vm_count'] or 0)
            powered_on = int(row['powered_on'] or 0)
            vcpus_allocated = int(row['total_vcpus_allocated'] or 0)

        avail_mem = (host['total_mem_gb'] or 0) - (host['used_mem_gb'] or 0)
        avail_storage = (host['total_storage_gb'] or 0) - (host['used_storage_gb'] or 0)

        availability.append({
            "ip": host['ip'],
            "group": host['group_name'],
            "total_cpu_ghz": round(host['total_cpu_ghz'] or 0, 1),
            "cpu_usage_pct": round(host['cpu_usage'] or 0, 1),
            "total_mem_gb": round(host['total_mem_gb'] or 0, 1),
            "avail_mem_gb": round(max(avail_mem, 0), 1),
            "mem_usage_pct": round(host['mem_usage'] or 0, 1),
            "total_storage_gb": round(host['total_storage_gb'] or 0, 1),
            "avail_storage_gb": round(max(avail_storage, 0), 1),
            "storage_usage_pct": round(host['storage_usage'] or 0, 1),
            "vm_count": vm_count,
            "powered_on_vms": powered_on,
            "vcpus_allocated": vcpus_allocated,
        })

    return availability


def compute_deployment_recommendations(reqs, availability):
    """Compute single-host fits and distributed deployment plan."""
    single_fits = []
    for host in availability:
        can_fit_ram = (reqs["ram_gb"] == 0) or (host["avail_mem_gb"] >= reqs["ram_gb"])
        can_fit_storage = (reqs["storage_gb"] == 0) or (host["avail_storage_gb"] >= reqs["storage_gb"])

        if can_fit_ram and can_fit_storage:
            max_by_ram = int(host["avail_mem_gb"] / reqs["ram_gb"]) if reqs["ram_gb"] > 0 else 999
            max_by_storage = int(host["avail_storage_gb"] / reqs["storage_gb"]) if reqs["storage_gb"] > 0 else 999
            max_vms = min(max_by_ram, max_by_storage)

            avg_usage = (host["cpu_usage_pct"] + host["mem_usage_pct"]) / 2
            fitness_score = round(100 - avg_usage, 1)

            single_fits.append({
                **host,
                "max_vms_fit": max_vms,
                "can_fit_all": max_vms >= reqs["count"],
                "fitness_score": fitness_score,
            })

    single_fits.sort(key=lambda x: (-x["can_fit_all"], -x["fitness_score"]))

    # Distributed plan for multi-VM deployments
    distributed = []
    if reqs["count"] > 1:
        remaining = reqs["count"]
        candidates = sorted(
            [h for h in single_fits if h["max_vms_fit"] > 0],
            key=lambda x: -x["fitness_score"]
        )
        for host in candidates:
            if remaining <= 0:
                break
            assign = min(remaining, host["max_vms_fit"])
            distributed.append({
                "Host IP": host["ip"],
                "Group": host["group"],
                "VMs to Deploy": assign,
                "Avail RAM (GB)": host["avail_mem_gb"],
                "Avail Storage (GB)": host["avail_storage_gb"],
                "CPU %": host["cpu_usage_pct"],
                "Mem %": host["mem_usage_pct"],
            })
            remaining -= assign

        if remaining > 0:
            distributed.append({
                "Host IP": "INSUFFICIENT CAPACITY",
                "Group": "-",
                "VMs to Deploy": remaining,
                "Avail RAM (GB)": 0,
                "Avail Storage (GB)": 0,
                "CPU %": 0,
                "Mem %": 0,
            })

    return single_fits, distributed


def handle_deploy_recommendation(user_input):
    """Parse requirements, compute fits, present recommendations."""
    reqs = parse_deployment_requirements(user_input)

    if reqs is None:
        st.info("I'd love to help with deployment planning! Please specify the resources, for example:\n\n"
                "*\"I need to deploy a VM with 4 vCPUs, 16GB RAM, and 100GB storage\"*\n\n"
                "*\"Deploy 3 machines with 8GB RAM and 50GB disk each\"*")
        return "[Waiting for resource specifications]"

    # Show parsed requirements
    req_parts = []
    if reqs["vcpus"]: req_parts.append(f"{reqs['vcpus']} vCPUs")
    if reqs["ram_gb"]: req_parts.append(f"{reqs['ram_gb']} GB RAM")
    if reqs["storage_gb"]: req_parts.append(f"{reqs['storage_gb']} GB Storage")
    count_label = f" x{reqs['count']} instances" if reqs["count"] > 1 else ""
    st.info(f"**Requirements:** {' | '.join(req_parts)}{count_label}")

    with st.spinner("Analyzing host availability..."):
        availability = get_host_availability()
        single_fits, distributed = compute_deployment_recommendations(reqs, availability)

    if not single_fits:
        st.error("No hosts have sufficient resources for this workload.")
        st.markdown("**Current Host Availability:**")
        avail_df = pd.DataFrame(availability)[["ip", "group", "avail_mem_gb", "avail_storage_gb", "cpu_usage_pct", "mem_usage_pct"]]
        avail_df.columns = ["Host IP", "Group", "Avail RAM (GB)", "Avail Storage (GB)", "CPU %", "Mem %"]
        st.dataframe(avail_df, width='stretch')
        return "[No hosts with sufficient resources]"

    # Top recommendations
    st.markdown("### Recommended Hosts")
    top = single_fits[:5]
    rec_df = pd.DataFrame(top)[["ip", "group", "avail_mem_gb", "avail_storage_gb",
                                 "cpu_usage_pct", "mem_usage_pct", "max_vms_fit", "fitness_score"]]
    rec_df.columns = ["Host IP", "Group", "Avail RAM (GB)", "Avail Storage (GB)",
                      "CPU %", "Mem %", "Max VMs Fit", "Fitness Score"]
    st.dataframe(rec_df, width='stretch')

    # Distributed plan
    if distributed and reqs["count"] > 1:
        st.markdown("### Distributed Deployment Plan")
        st.dataframe(pd.DataFrame(distributed), width='stretch')

    # LLM summary
    top3_summary = json.dumps(top[:3], indent=2, default=str)
    summary_prompt = f"""The user asked: "{user_input}"
They need: {', '.join(req_parts)}{count_label}.

Top recommended hosts:
{top3_summary}

Write a brief, friendly recommendation (2-4 sentences). Mention the best host by IP, why it's best (lowest utilization/most headroom), and any warnings if resources are tight."""

    st.markdown("---")
    placeholder = st.empty()
    full_text = ""
    for token in call_llm_streaming(summary_prompt, system=SYSTEM_PROMPT_CHAT):
        full_text += token
        placeholder.markdown(full_text)
    return full_text

# =============================================================================
# Main Render Function
# =============================================================================

def get_predictive_analysis():
    """Extracts 30-day host trends and performs predictive capacity analysis via Gemini."""
    try:
        conn = sqlite3.connect(DB_PATH)
        # Fetch daily averages for the last 30 days
        trend_query = """
            SELECT 
                e.ip,
                date(m.last_updated) as day,
                AVG(m.cpu_usage) as avg_cpu,
                AVG(m.mem_usage) as avg_mem,
                AVG(m.storage_usage) as avg_storage
            FROM host_metrics m
            JOIN esxi_hosts e ON m.host_id = e.id
            WHERE m.last_updated >= date('now', '-30 days')
            GROUP BY e.ip, day
            ORDER BY day ASC
        """
        trends_df = pd.read_sql_query(trend_query, conn)
        conn.close()

        if trends_df.empty:
            return "Insufficient historical data for predictive analysis. Please allow the background collector to run for a few days."

        # Summarize trends for the prompt
        trend_summary = trends_df.groupby('ip').agg({
            'avg_cpu': ['mean', 'last'],
            'avg_mem': ['mean', 'last'],
            'avg_storage': ['mean', 'last']
        }).to_string()

        prompt = f"""You are an Infrastructure Architect. Perform a Predictive Capacity Analysis based on these 30-day ESXi host trends:
        
        TREND DATA (Daily Averages):
        {trend_summary}
        
        TASK:
        1. Calculate the 'Resource Runway' (estimated days until 100% exhaustion) for CPU, RAM, and Storage per host.
        2. Identify specific bottlenecks.
        3. Recommend VM placement optimization (e.g., 'Move high-RAM VMs from Host A to Host B').
        4. Provide a 'Cluster Health Score' (0-100).
        
        Format your response in professional Markdown with a 'Resource Runway' table. Be precise and data-driven."""

        model = _get_gemini_model(system="You are an expert Virtualization Architect specializing in VMware and capacity planning.")
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"Error during predictive analysis: {str(e)}"

def render_ai_agent():
    """Entry point called from monitoring_dashboard.py."""
    st.title("🧠 AI Infrastructure Agent")
    st.caption(f"Backend: Gemini ({GEMINI_MODEL})")

    init_chat_history()

    # Clear chat and Predictive Analysis buttons in sidebar
    with st.sidebar:
        if st.button("Clear Chat", key="clear_agent_chat", width='stretch'):
            st.session_state.agent_chat_history = []
            st.rerun()
            
        st.divider()
        st.subheader("Advanced Analysis")
        if st.button("🚀 Run Predictive Capacity Analysis", width='stretch'):
            with st.spinner("Analyzing 30-day trends..."):
                report = get_predictive_analysis()
                add_to_history("assistant", report)
                st.rerun()

    render_chat_history()

    if user_input := st.chat_input("Ask about your infrastructure..."):
        with st.chat_message("user"):
            st.markdown(user_input)
        add_to_history("user", user_input)

        intent = classify_intent(user_input)

        with st.chat_message("assistant"):
            if intent == "sql_query":
                response_summary = handle_sql_query(user_input, st.session_state.agent_chat_history)
            elif intent == "deploy_recommend":
                response_summary = handle_deploy_recommendation(user_input)
            elif intent == "infra_analysis":
                response_summary = handle_infra_analysis(user_input)
            elif intent == "general_chat":
                response_summary = handle_general_chat(user_input, st.session_state.agent_chat_history)
            else:
                response_summary = handle_sql_query(user_input, st.session_state.agent_chat_history)

        add_to_history("assistant", response_summary or "[Response rendered]")


if __name__ == "__main__":
    print("AI Agent module loaded.")
    print("Testing intent classifier...")
    tests = [
        ("Show me all VMs on host 192.168.1.7", "sql_query"),
        ("Deploy a VM with 8 vCPUs and 32GB RAM", "deploy_recommend"),
        ("How is my infrastructure doing?", "infra_analysis"),
        ("Hello!", "general_chat"),
        ("I need to place 3 machines with 16GB RAM each", "deploy_recommend"),
        ("List all powered off VMs", "sql_query"),
        ("What can you do?", "general_chat"),
        ("Show me the utilization across all hosts", "sql_query"),
        ("Which VMs are using the most RAM?", "sql_query"),
        ("What machines contribute to the overload?", "sql_query"),
        ("Show me host resource usage per group", "sql_query"),
        ("Give me an infrastructure summary", "infra_analysis"),
        ("What IPs are down?", "sql_query"),
        ("Show me storage usage for each VM", "sql_query"),
        ("Overall infrastructure health report", "infra_analysis"),
    ]
    for question, expected in tests:
        result = classify_intent(question)
        status = "PASS" if result == expected else "FAIL"
        print(f"  [{status}] '{question}' -> {result} (expected {expected})")
