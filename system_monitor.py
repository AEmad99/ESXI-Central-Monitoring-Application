import psutil
import os
import time

def get_system_stats():
    """Returns a dictionary of current system resource usage."""
    cpu_percent = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    # Get load average
    load1, load5, load15 = os.getloadavg() if hasattr(os, 'getloadavg') else (0,0,0)
    
    return {
        "cpu_usage_pct": cpu_percent,
        "mem_used_gb": memory.used / (1024**3),
        "mem_total_gb": memory.total / (1024**3),
        "mem_usage_pct": memory.percent,
        "disk_usage_pct": disk.percent,
        "load_1m": load1
    }

def get_process_stats():
    """Returns stats for the current process."""
    process = psutil.Process(os.getpid())
    return {
        "proc_cpu_pct": process.cpu_percent(interval=None),
        "proc_mem_mb": process.memory_info().rss / (1024**2)
    }
