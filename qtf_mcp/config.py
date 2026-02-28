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
