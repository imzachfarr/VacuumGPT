import asyncio
from pathlib import Path
import sys
import unittest
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

class ConnectorTests(unittest.TestCase):
    def test_stdio_discovery_read_and_unapproved_command_refusal(self):
        async def check():
            server=Path(__file__).resolve().parents[1]/'runtime/observer_mcp.py'
            params=StdioServerParameters(command=sys.executable,args=[str(server)])
            async with stdio_client(params) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    listing=await session.list_tools()
                    names={tool.name for tool in listing.tools}
                    self.assertTrue({'roomba_status','roomba_name_room','roomba_command','roomba_clean_room','roomba_create_schedule'}<=names)
                    rooms=await session.call_tool('roomba_rooms',{})
                    self.assertFalse(rooms.isError)
                    blocked=await session.call_tool('roomba_command',{'action':'start','request_id':'offline-package-test'})
                    self.assertTrue(blocked.isError)
        asyncio.run(check())
