import os
import sys
import asyncio
from unittest.mock import MagicMock

# Add project root to sys.path
sys.path.append(os.getcwd())

from qtf_mcp.mcp_app import full
from mcp.server.fastmcp import Context

async def test():
    # Mocking context
    ctx = MagicMock(spec=Context)
    # Set the depth correctly for the mock
    class MockClient: host = "localhost"
    class MockRequest: client = MockClient()
    class MockRequestContext: request = MockRequest()
    ctx.request_context = MockRequestContext()
    
    result = await full("SH603986", ctx)
    print(result[:500]) # just a preview

if __name__ == "__main__":
    asyncio.run(test())
