"""
Configuration settings for the QTF MCP server.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# AkShare Proxy Patch Configuration
AKSHARE_PROXY_IP = os.getenv("AKSHARE_PROXY_IP")
AKSHARE_PROXY_PASSWORD = os.getenv("AKSHARE_PROXY_PASSWORD")
AKSHARE_PROXY_PORT = int(os.getenv("AKSHARE_PROXY_PORT", "0"))

# --- Market Indices Configuration ---
import json
_CONF_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "confs"))
_INDICES_FILE = os.path.join(_CONF_DIR, "indices.json")

SH_INDICES: set[str] = set()
SZ_INDICES: set[str] = set()
ALL_INDICES: set[str] = set()

try:
    if os.path.exists(_INDICES_FILE):
        with open(_INDICES_FILE, "r", encoding="utf-8") as f:
            _conf = json.load(f)
            SH_INDICES = set(_conf.get("sh_indices", []))
            SZ_INDICES = set(_conf.get("sz_indices", []))
            ALL_INDICES = SH_INDICES | SZ_INDICES
except Exception:
    # 基础兜底名单
    SH_INDICES = {"000001", "000300", "000016", "000905", "000688", "000852"}
    SZ_INDICES = {"399001", "399006", "399005", "399300", "399007"}
    ALL_INDICES = SH_INDICES | SZ_INDICES
