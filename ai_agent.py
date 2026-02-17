import sqlite3
import requests
import json
import re
import pandas as pd
import streamlit as st

# Configuration
DB_PATH = "monitoring.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:4b"  # Upgraded Smarter Model

# Enhanced SQL Generation Prompt for Deep Context Awareness
SQL_GENERATION_PROMPT = """You are a highly intelligent SQL generator for an ESXi Virtualization environment.
Your task is to translate natural language into accurate SQLite queries based on the infrastructure context.

### INFRASTRUCTURE CONTEXT (SCHEMA):
1. TABLE `esxi_hosts`: These are the PHYSICAL servers (the hardware).
   - `id`: Primary Key.
   - `ip`: The management IP of the physical server.
   - `username`: Admin username.
   - `group_name`: The logical cluster (e.g., 'Production', 'Lab').

2. TABLE `vms`: These are the VIRTUAL machines running inside the hosts.
   - `id`: Primary Key.
   - `host_id`: Foreign Key linking to `esxi_hosts.id`.
   - `name`: The display name of the VM.
   - `ip`: The network IP(s) of the VM.
   - `power_state`: Status (ALWAYS 'poweredOn' or 'poweredOff').
   - `os`: Operating System (e.g., 'RHEL', 'Windows').
   - `cpu_count`: Number of vCPUs assigned.

3. TABLE `host_metrics`: Real-time performance of PHYSICAL hosts.
   - `host_id`: Link to `esxi_hosts.id`.
   - `cpu_usage` / `mem_usage`: Percentage (0-100).

### SEMANTIC MAPPING RULES:
- "Machines", "VMs", "Guests" -> Reference the `vms` table.
- "Physical", "Servers", "Hypervisors", "Hosts" -> Reference the `esxi_hosts` table.
- To find which host a VM is on: `JOIN esxi_hosts ON vms.host_id = esxi_hosts.id`.
- For ANY search involving names (like "Cognos", "App", "DB"), ALWAYS use `LIKE '%term%'` to be safe.
- If the user asks for "IPs", select `vms.ip` and `esxi_hosts.ip`. 
- MANDATORY: Alias duplicate columns. Use `vms.ip AS vm_ip` and `esxi_hosts.ip AS host_ip`.

### OUTPUT INSTRUCTIONS:
- Output ONLY the SQL query.
- Wrap the query in [SQL] and [/SQL] tags.
- NO markdown, NO preamble, NO explanation.

Question: {question}
SQL:"""

def execute_sql(query):
    """Executes SQL and handles column aliasing for Streamlit."""
    try:
        # Robust cleaning of LLM artifacts
        query = re.sub(r"```sql\n?|```\n?", "", query, flags=re.IGNORECASE).strip()
        # Remove "SQL" label if model adds it at the start
        if query.upper().startswith("SQL"):
            query = query[3:].strip()
        
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # Clean duplicate column names
        cols = pd.Series(df.columns)
        for dup in cols[cols.duplicated()].unique(): 
            cols[cols[cols == dup].index.values.tolist()] = [dup + '_' + str(i) if i != 0 else dup for i in range(sum(cols == dup))]
        df.columns = cols
        
        return df
    except Exception as e:
        return f"SQL Error: {str(e)}"

def get_sql_from_llm(question):
    """Synchronous call to Ollama using the new Gemma 3 model."""
    payload = {
        "model": MODEL,
        "prompt": SQL_GENERATION_PROMPT.format(question=question),
        "stream": False,
        "options": {
            "temperature": 0, 
            "num_thread": 12, 
            "keep_alive": -1,
            "num_ctx": 4096
        }
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        text = response.json().get("response", "").strip()
        
        # Priority 1: [SQL] tags (Mandated by prompt)
        sql_tag_match = re.search(r"\[SQL\](.*?)\[/SQL\]", text, re.DOTALL | re.IGNORECASE)
        if sql_tag_match:
            return sql_tag_match.group(1).strip()
            
        # Priority 2: Markdown code blocks
        code_match = re.search(r"```(?:sql)?\n?(.*?)\n?```", text, re.DOTALL)
        if code_match:
            return code_match.group(1).strip()
            
        return text # Fallback
    except Exception as e:
        return None

def render_ai_agent():
    st.title("🤖 Intelligent Infrastructure Agent")
    st.info(f"Engine: {MODEL} | Precision Mode Active")

    if user_input := st.chat_input("Ask about your environment... (e.g., 'Show me all Cognos IPs')"):
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner(f"Gemma 3 is reasoning..."):
                query = get_sql_from_llm(user_input)
            
            if query:
                st.code(query, language="sql")
                
                with st.spinner("Executing optimized query..."):
                    df = execute_sql(query)
                
                if isinstance(df, pd.DataFrame):
                    if not df.empty:
                        st.success(f"Success: {len(df)} records retrieved.")
                        st.dataframe(df, use_container_width=True)
                    else:
                        st.warning("Query executed successfully, but no records matched.")
                else:
                    st.error(df)
            else:
                st.error("Gemma 3 failed to generate a valid SQL query. Try rephrasing.")

if __name__ == "__main__":
    # Test Block
    print(f"Spinning up {MODEL} for verification...")
    q = "how many cognos machines do we have? return name, ip and host ip"
    sql = get_sql_from_llm(q)
    print(f"Generated SQL: {sql}")
    if sql:
        res = execute_sql(sql)
        print("\n--- RESULTS ---")
        print(res)
