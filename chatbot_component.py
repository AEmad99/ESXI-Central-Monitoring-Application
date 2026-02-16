import streamlit as st
import pandas as pd
import ollama
import database
from models import ESXiHost, VM, IPLease, HostMetrics, NetworkDevice

# --- CONFIGURATION ---
DEFAULT_MODEL = "qwen2.5-coder:3b" # The "Goldilocks" model: Balanced Speed & Accuracy

@st.cache_data(ttl=60)
def get_dataframes():
    """Fetches key tables into Pandas DataFrames for analysis. Cached for 60s."""
    db = database.get_session()
    try:
        # 1. Hosts & Metrics
        hosts = pd.read_sql(db.query(ESXiHost).statement, db.bind)
        metrics = pd.read_sql(db.query(HostMetrics).statement, db.bind)
        if not hosts.empty and not metrics.empty:
            df_hosts = pd.merge(hosts, metrics, left_on='id', right_on='host_id', how='left')
            # Pre-calculate capacity for easier LLM querying
            df_hosts['free_cpu_ghz'] = df_hosts['total_cpu_ghz'] - df_hosts['used_cpu_ghz']
            df_hosts['free_mem_gb'] = df_hosts['total_mem_gb'] - df_hosts['used_mem_gb']
            df_hosts['free_storage_gb'] = df_hosts['total_storage_gb'] - df_hosts['used_storage_gb']
            
            # Select useful columns - KEY FIX: Include 'id' as 'host_id' for joining
            df_hosts = df_hosts.rename(columns={'id': 'host_id'})
            cols = ['host_id', 'ip', 'username', 'cpu_usage', 'used_cpu_ghz', 'total_cpu_ghz', 'free_cpu_ghz',
                    'mem_usage', 'used_mem_gb', 'total_mem_gb', 'free_mem_gb', 
                    'storage_usage', 'total_storage_gb', 'free_storage_gb']
            # Filter cols that exist
            cols = [c for c in cols if c in df_hosts.columns]
            df_hosts = df_hosts[cols].fillna(0)
        else:
            df_hosts = hosts  # Fallback
            
        # 2. Virtual Machines
        df_vms = pd.read_sql(db.query(VM).statement, db.bind)
        
        # Parse 'ram_info' (e.g. "10813 / 98304 MB") into numeric columns
        if not df_vms.empty and 'ram_info' in df_vms.columns:
             # Extract numbers using regex
             try:
                 mem_data = df_vms['ram_info'].str.extract(r'(\d+)\s*/\s*(\d+)\s*MB')
                 df_vms['vm_used_mem_mb'] = pd.to_numeric(mem_data[0], errors='coerce').fillna(0)
                 df_vms['vm_total_mem_mb'] = pd.to_numeric(mem_data[1], errors='coerce').fillna(0)
             except Exception:
                 df_vms['vm_used_mem_mb'] = 0.0
                 df_vms['vm_total_mem_mb'] = 0.0

        # Select useful columns
        vm_cols = ['name', 'os', 'ip', 'cpu_count', 'ram_info', 'vm_used_mem_mb', 'vm_total_mem_mb', 'power_state', 'host_id']
        if not df_vms.empty:
            existing_cols = [c for c in vm_cols if c in df_vms.columns]
            df_vms = df_vms[existing_cols]
            
        # 3. Network / IPs
        df_network = pd.read_sql(db.query(IPLease).statement, db.bind)
        
    except Exception as e:
        # st.error(f"Error loading data: {e}") # Suppress partial errors during cache refresh
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    finally:
        db.close()
        
    return df_hosts, df_vms, df_network

def build_data_profile(df, name):
    """Generates a rich text summary of the dataframe for the LLM context."""
    if df.empty:
        return f"{name} is empty."
    
    profile = [f"DataFrame: `{name}`"]
    profile.append(f"- Columns: {list(df.columns)}")
    
    # 1. Categorical Summary (Optimized: Only scan relevant identifier columns)
    # detecting small cardinality columns for filtering context
    for col in df.columns:
        if df[col].dtype == 'object' or df[col].dtype.name == 'category':
            if "name" in col or "ip" in col: continue # Skip high cardinality names/IPs to save tokens
            
            try:
                unique_vals = df[col].dropna().unique()
                if len(unique_vals) < 10: # Only list if very few options (e.g. power_state)
                    profile.append(f"  - `{col}` allowed values: {list(unique_vals)}")
            except:
                pass

    # 2. Numerical Summary (Ranges)
    num_cols = df.select_dtypes(include=['number']).columns
    for col in num_cols:
        if "id" in col: continue # Skip IDs
        try:
            min_v = df[col].min()
            max_v = df[col].max()
            profile.append(f"  - `{col}` range: {min_v} to {max_v}")
        except:
            pass
        
    profile.append(f"- Sample Rows:\n{df.head(2).to_string()}")
    return "\n".join(profile)

def generate_python_code(model_name, question, df_hosts, df_vms, df_network, error_feedback=None):
    """
    Asks the LLM to write Python Pandas code to answer the question.
    """
    # Context Construction (RAG-lite)
    context_parts = []
    context_parts.append(build_data_profile(df_hosts, "df_hosts"))
    context_parts.append(build_data_profile(df_vms, "df_vms"))
    context_parts.append(build_data_profile(df_network, "df_network"))
        
    context_str = "\n\n".join(context_parts)
    
    system_prompt = (
        "You are a Python Data Analyst for an ESXi Infrastructure. You have IMMEDIATE access to three pandas DataFrames: `df_hosts`, `df_vms`, and `df_network`. They are already loaded in memory.\n"
        "Your task is to Write Python code to answer the user's question.\n"
        "Rules:\n"
        "1. DO NOT load any files. DO NOT use pd.read_csv(). The dataframes `df_hosts`, `df_vms`, and `df_network` EXIST IN MEMORY.\n"
        "2. DO NOT create new dataframes with sample data. DO NOT write `df_hosts = ...` or `df_vms = ...`. ASSUME THEY EXIST.\n"
        "3. The final answer must be assigned to a variable named `result`.\n"
        "4. `result` can be a DataFrame, a Series, a list, a number, or a string.\n"
        "5. Do NOT use print().\n"
        "6. Do NOT import any libraries (pandas is already imported as pd).\n"
        "7. Return ONLY the code block. No explanations, no markdown formatting (like ```python).\n"
        "\n"
        "BUSINESS LOGIC & GLOSSARY (Use this to map user terms to data):\n"
        "- 'Machines', 'VMs', 'Guest OS' -> Refer to `df_vms`. (Columns: `name`, `os`, `power_state`).\n"
        "- 'Hosts', 'Servers', 'Nodes', 'ESXi' -> Refer to `df_hosts`. (Columns: `ip`, `cpu_usage`, `mem_usage`).\n"
        "- 'Running', 'Active', 'Up' -> For VMs: `power_state == 'poweredOn'`. For Hosts: Always assumed active if in this list.\n"
        "- 'Utilization'/'Usage' -> Use `used_` columns (e.g. `used_mem_gb`).\n"
        "- 'Free'/'Available' -> Use `free_` columns (e.g. `free_mem_gb`).\n"
        "- 'Capacity' -> Use `total_` columns.\n"
        "IMPORTANT: `df_hosts` does NOT contain `power_state`. `df_vms` does NOT contain `cpu_usage` (it has `cpu_count`).\n"
    )
    
    # Smart Entity Linking: Check if IPs/names in the question map to Hosts or VMs
    import re
    ip_matches = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', question)
    entity_hints = []
    
    for ip in ip_matches:
        if not df_hosts.empty and ip in df_hosts['ip'].values:
            entity_hints.append(f"HINT: The IP '{ip}' belongs to an ESXi HOST. Use `df_hosts`. Use `used_mem_gb` for memory.")
        elif not df_vms.empty and ip in df_vms['ip'].values:
            entity_hints.append(f"HINT: The IP '{ip}' belongs to a Virtual Machine (VM). Use `df_vms`. Use `vm_used_mem_mb` for memory.")
            
    hint_str = "\n".join(entity_hints)
    
    if error_feedback:
        user_prompt = f"Data Context:\n{context_str}\n\nPREVIOUS CODE FAILED with error:\n{error_feedback}\n\nPlease FIX the code to answer: {question}\n\nCorrected Python Code:"
    else:
        user_prompt = f"Data Context:\n{context_str}\n\n{hint_str}\n\nQuestion: {question}\n\nPython Code:"
    
    try:
        response = ollama.chat(model=model_name, messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ])
        code = response['message']['content']
        
        # Robust Code Extraction (Regex)
        import re
        # Find code blocks ```python ... ``` or just ``` ... ```
        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        if match:
            code = match.group(1).strip()
        else:
            # Fallback: specific cleanup if no blocks found
            code = code.replace("```python", "").replace("```", "").strip()
            
        return code
    except Exception as e:
        return f"Error calling LLM: {e}"

def summarize_result(model_name, question, result_obj, generated_code=""):
    """
    Asks the LLM to summarize the result in natural language.
    """
    # Enhance result string with explicit counts and CSV format to prevent hallucination
    meta_info = ""
    result_str = str(result_obj)

    if isinstance(result_obj, (pd.DataFrame, pd.Series)):
        meta_info = f"Metadata: Total Rows/Items = {len(result_obj)}\n"
        if isinstance(result_obj, pd.DataFrame):
            meta_info += f"Columns: {list(result_obj.columns)}\n"
            # Use CSV format for STRICT column alignment (prevents whitespace hallucinations)
            try:
                result_str = result_obj.to_csv(index=False)
            except:
                result_str = result_obj.to_string(index=False)

    # Truncate if too long
    if len(result_str) > 4000:
        result_str = result_str[:4000] + "\n... (data truncated for length)"
        
    prompt = (
        f"User Question: {question}\n\n"
        f"Analyzed using Code:\n{generated_code}\n\n"
        f"{meta_info}"
        f"Data Result (CSV Format):\n{result_str}\n\n"
        "Please provide a concise, natural language answer based on the data result above. "
        "The data is in CSV format, so rely on the column headers explicitly. "
        "Check the 'Analyzed using Code' section to understand if the unit is GB, MB, or GHz based on column names (e.g. `_gb`, `_mb`)."
        "If there is a 'Total Rows' count in Metadata, USE IT as the rigorous answer for 'how many'. "
        "Do not mention 'the dataframe' or 'technical details'. Just give the answer."
    )
    
    try:
        response = ollama.chat(model=model_name, messages=[
            {'role': 'user', 'content': prompt},
        ])
        return response['message']['content']
    except Exception as e:
        return f"Error summarizing: {e}"

def render_chatbot():
    st.title("🤖 Local Data Analyst")
    st.caption("Powered by Ollama (Local LLM) • Code Generation Agent")

    # Model Selection
    model_name = st.sidebar.text_input("Local Model Name", value=DEFAULT_MODEL, help="Ensure this model is pulled in Ollama (e.g. `ollama pull qwen2.5-coder:7b`)")

    # Session State
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display History
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if "code" in msg:
                with st.expander("Show Generated Code"):
                    st.code(msg["code"], language="python")
            if "data" in msg:
                 with st.expander("Show Raw Data"):
                     st.dataframe(msg["data"])

    # Input
    if prompt := st.chat_input("Ask a question about your infrastructure..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            status_container = st.empty()
            
            # 1. Fetch Data
            status_container.markdown("🔄 Fetching live data...")
            df_hosts, df_vms, df_network = get_dataframes()
            
            # 2. Generate Code (First Attempt)
            status_container.markdown("🧠 Generating analysis code...")
            generated_code = generate_python_code(model_name, prompt, df_hosts, df_vms, df_network)
            
            error_message = None
            result = None
            
            # 3. Execute Code (With Retry Loop)
            max_retries = 1
            for attempt in range(max_retries + 1):
                status_container.markdown(f"⚙️ Executing code... (Attempt {attempt+1})")
                local_vars = {
                    "pd": pd,
                    "df_hosts": df_hosts,
                    "df_vms": df_vms,
                    "df_network": df_network,
                    "result": None
                }
                
                try:
                    exec(generated_code, {}, local_vars)
                    result = local_vars.get("result")
                    error_message = None # Success
                    break # Exit loop
                except Exception as e:
                    error_message = str(e)
                    if attempt < max_retries:
                        status_container.markdown(f"⚠️ Code failed ({e}). Self-correcting...")
                        generated_code = generate_python_code(model_name, prompt, df_hosts, df_vms, df_network, error_feedback=error_message)
                    else:
                        pass # Failed final attempt

            if error_message:
                status_container.error(f"Analysis Failed after retries: {error_message}")
                st.code(generated_code, language="python")
                st.session_state.messages.append({"role": "assistant", "content": f"I couldn't generate working code. Error: {error_message}"})
                return

            # 4. Summarize
            status_container.markdown("📝 Summarizing results...")
            final_answer = summarize_result(model_name, prompt, result, generated_code=generated_code)
            
            status_container.markdown(final_answer)
            
            # Optional: Show Data/Code
            with st.expander("View Analysis Details"):
                st.code(generated_code, language="python")
                if isinstance(result, (pd.DataFrame, pd.Series)):
                    st.dataframe(result)
                else:
                    st.write(result)
            
            # Save to history
            msg_data = {"role": "assistant", "content": final_answer, "code": generated_code}
            if isinstance(result, (pd.DataFrame, pd.Series)):
                msg_data["data"] = result
            st.session_state.messages.append(msg_data)




