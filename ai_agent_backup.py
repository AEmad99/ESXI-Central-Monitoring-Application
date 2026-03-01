import sqlite3
import requests
import json
import re
import pandas as pd
import streamlit as st

# Configuration
DB_PATH = "monitoring.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"

# Highly accurate SQL generation prompt
SQL_GENERATION_PROMPT = """System: You are a SQLite expert. 
Generate a raw SQL query to answer the user's question based on the schema below.
Output ONLY the SQL query wrapped in [SQL] and [/SQL] tags.

### SCHEMA:
- esxi_hosts (id, ip, username, group_name)
- vms (id, host_id, name, os, ip, cpu_count, power_state)

### RELATIONSHIPS:
- vms.host_id = esxi_hosts.id

### RULES:
- Use `vms.ip` for VM IP and `esxi_hosts.ip` for Host IP.
- ALWAYS alias duplicate column names: `SELECT vms.ip AS vm_ip, esxi_hosts.ip AS host_ip ...`
- Use `LIKE '%term%'` for fuzzy name matching (e.g., WHERE vms.name LIKE '%Cognos%').
- Output ONLY the query. No explanation.
"""

def execute_sql(query):
    """Executes SQL and handles column aliasing for Streamlit."""
    try:
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
    """Synchronous call to Ollama to get the SQL query."""
    payload = {
        "model": MODEL,
        "prompt": f"{SQL_GENERATION_PROMPT}\n\nQuestion: {question}\nSQL:",
        "stream": False,
        "options": {"temperature": 0, "num_thread": 12, "keep_alive": -1}
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        text = response.json().get("response", "").strip()
        match = re.search(r"\[SQL\](.*?)\[/SQL\]", text, re.DOTALL)
        return match.group(1).strip() if match else None
    except Exception as e:
        return None

def render_ai_agent():
    st.title("🤖 Infrastructure Query Agent")
    st.info("Direct SQL execution mode enabled for maximum accuracy.")

    if user_input := st.chat_input("Ask about your infrastructure (e.g., 'List all Cognos machines')"):
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner("Generating Query..."):
                query = get_sql_from_llm(user_input)
            
            if query:
                st.code(query, language="sql")
                
                with st.spinner("Fetching Data..."):
                    df = execute_sql(query)
                
                if isinstance(df, pd.DataFrame):
                    if not df.empty:
                        st.success(f"Found {len(df)} results.")
                        st.dataframe(df, width='stretch')
                    else:
                        st.warning("No records found in the database.")
                else:
                    st.error(df)
            else:
                st.error("Failed to generate a valid SQL query for this request.")

if __name__ == "__main__":
    print("Agent logic loaded.")
