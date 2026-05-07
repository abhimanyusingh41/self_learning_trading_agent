import os
from dotenv import load_dotenv

load_dotenv()

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "changeme")
DATA_DIR = os.getenv("DATA_DIR", "..")
MEMORY_FILE = os.path.join(DATA_DIR, "data", "memory", "trade_memory.json")
LOG_FILE = os.path.join(DATA_DIR, "logs", "trading_agent.log")
