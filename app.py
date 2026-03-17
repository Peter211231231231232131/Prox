#!/usr/bin/env python3
import asyncio
import logging
import os
import random
import time
import aiohttp
import base64
import json
from urllib.parse import urlparse, parse_qs

from aiohttp import web
import websockets

# ---------- Configuration ----------
PROXY_TARGET = os.environ.get('PROXY_TARGET', 'pool.supportxmr.com:5555')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')
PORT = int(os.environ.get('PORT', 10000))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', 500))

# Shadowsocks node sources (updated frequently)
NODE_SOURCES = [
    "https://raw.githubusercontent.com/xyfqzy/free-nodes/main/nodes/shadowsocks.txt",
    "https://raw.githubusercontent.com/freefq/free/master/v2",
    "https://raw.githubusercontent.com/wrfree/free/main/ssr"
]

NODE_REFRESH_INTERVAL = 3600  # Refresh nodes every hour
SHADOWSOCKS_ENABLED = os.environ.get('SHADOWSOCKS_ENABLED', 'true').lower() == 'true'

# Fake site content
FAKE_SITE = """
<!DOCTYPE html>
<html>
<head><title>Push Notification API</title></head>
<body>
<h1>Push Notification Service v2.3</h1>
<p>WebSocket endpoint: /api/v1/push</p>
<p>Status: <span style="color:green">operational</span></p>
</body>
</html>
"""

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('obfuscated-proxy')

# ---------- Shadowsocks Node Manager ----------
class ShadowsocksNodeManager:
    def __init__(self):
        self.nodes = []
        self.current_node = None
        self.last_refresh = 0
        
    async def fetch_nodes(self):
        """Fetch Shadowsocks nodes from various sources"""
        all_nodes = []
        
        for source in NODE_SOURCES:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(source, timeout=10) as resp:
                        if resp.status == 200:
                            content = await resp.text()
                            nodes = self.parse_nodes(content)
                            all_nodes.extend(nodes)
                            logger.info(f"Fetched {len(nodes)} nodes from {source}")
            except Exception as e:
                logger.error(f"Failed to fetch from {source}: {e}")
        
        if all_nodes:
            self.nodes = all_nodes
            logger.info(f"Total nodes available: {len(self.nodes)}")
        else:
            logger.warning("No nodes fetched, using fallback nodes")
            # Fallback hardcoded nodes if fetch fails
            self.nodes = self.get_fallback_nodes()
    
    def parse_nodes(self, content):
        """Parse Shadowsocks nodes from various formats"""
        nodes = []
        lines = content.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # Handle ss:// URLs
            if line.startswith('ss://'):
                nodes.append(self.parse_ss_url(line))
            # Handle base64 encoded lists
            else:
                try:
                    decoded = base64.b64decode(line).decode('utf-8')
                    if decoded.startswith('ss://'):
                        nodes.append(self.parse_ss_url(decoded))
                except:
                    pass
        
        return [n for n in nodes if n is not None]
    
    def parse_ss_url(self, ss_url):
        """Parse a Shadowsocks URL into connection parameters"""
        try:
            # Format: ss://method:password@host:port#tag
            # Or base64-encoded: ss://base64(method:password)@host:port#tag
            import urllib.parse
            
            parsed = urllib.parse.urlparse(ss_url)
            if parsed.scheme != 'ss':
                return None
            
            # Split the netloc part
            if '@' in parsed.netloc:
                auth, hostport = parsed.netloc.split('@', 1)
                
                # Try to decode auth if it's base64
                try:
                    decoded = base64.b64decode(auth).decode('utf-8')
                    if ':' in decoded:
                        method, password = decoded.split(':', 1)
                    else:
                        method = 'aes-256-gcm'  # default
                        password = decoded
                except:
                    # Not base64, might be plain method:password
                    if ':' in auth:
                        method, password = auth.split(':', 1)
                    else:
                        method = 'aes-256-gcm'
                        password = auth
                
                # Parse host and port
                if ':' in hostport:
                    host, port_str = hostport.split(':', 1)
                    port = int(port_str)
                else:
                    host = hostport
                    port = 443
                
                # Get tag from fragment
                tag = parsed.fragment if parsed.fragment else 'unknown'
                
                return {
                    'method': method,
                    'password': password,
                    'host': host,
                    'port': port,
                    'tag': tag,
                    'url': ss_url
                }
        except Exception as e:
            logger.debug(f"Failed to parse SS URL {ss_url}: {e}")
            return None
    
    def get_fallback_nodes(self):
        """Fallback hardcoded nodes if fetch fails"""
        return [
            {
                'method': 'chacha20-ietf-poly1305',
                'password': '36ZCHeabUSfKjfQEvJ4HDV',
                'host': '185.242.86.156',
                'port': 54170,
                'tag': 'fallback-russia'
            },
            {
                'method': 'aes-256-gcm',
                'password': 'd6105bbd-be0d-45b2-82ad-31fd1071c1d2',
                'host': 'service.ouluyun9803.com',
                'port': 20003,
                'tag': 'fallback-china'
            }
        ]
    
    async def refresh_nodes_if_needed(self):
        """Refresh nodes if interval has passed"""
        if time.time() - self.last_refresh > NODE_REFRESH_INTERVAL:
            await self.fetch_nodes()
            self.last_refresh = time.time()
    
    def get_random_node(self):
        """Get a random Shadowsocks node"""
        if not self.nodes:
            return None
        return random.choice(self.nodes)
    
    async def get_best_node(self):
        """Get the best node based on latency (simplified)"""
        if not self.nodes:
            await self.fetch_nodes()
        
        if self.nodes:
            # For simplicity, just return a random node
            # In a real implementation, you'd test latency
            return random.choice(self.nodes)
        return None

# Initialize node manager
node_manager = ShadowsocksNodeManager()

# ---------- Connection tracking ----------
active_connections = 0
semaphore = asyncio.Semaphore(MAX_CONNECTIONS)

# ---------- Shadowsocks Connection Helper ----------
class ShadowsocksConnection:
    """Simulates a Shadowsocks encrypted connection"""
    
    def __init__(self, node):
        self.node = node
        self.reader = None
        self.writer = None
    
    async def connect(self):
        """Connect through Shadowsocks node"""
        try:
            # In a real implementation, you'd use a Shadowsocks client library
            # For this example, we connect directly to the node and handle encryption ourselves
            # A proper implementation would use something like:
            # from shadowsocks import crypto, tcprelay
            
            # For now, we'll use a simple TCP connection to the node
            # In reality, you'd need to implement Shadowsocks protocol
            self.reader, self.writer = await asyncio.open_connection(
                self.node['host'], 
                self.node['port']
            )
            logger.info(f"Connected to Shadowsocks node: {self.node['tag']}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Shadowsocks node: {e}")
            return False
    
    async def send(self, data):
        """Send data through Shadowsocks (encrypted)"""
        if self.writer:
            # In real implementation, encrypt data here
            self.writer.write(data)
            await self.writer.drain()
    
    async def receive(self, buffer_size=4096):
        """Receive data through Shadowsocks (decrypted)"""
        if self.reader:
            data = await self.reader.read(buffer_size)
            # In real implementation, decrypt data here
            return data
        return None
    
    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

# ---------- WebSocket handler (with Shadowsocks routing) ----------
async def websocket_handler(request):
    global active_connections
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Optional token authentication
    if ACCESS_TOKEN:
        query = parse_qs(request.query_string)
        token = query.get('token', [None])[0]
        if token != ACCESS_TOKEN:
            logger.warning(f"Unauthorized attempt from {request.remote}")
            await ws.close()
            return ws

    async with semaphore:
        active_connections += 1
        client = request.remote
        logger.info(f"New client: {client} | Active: {active_connections}")

        # Get a Shadowsocks node
        ss_node = await node_manager.get_best_node()
        if not ss_node:
            logger.error("No Shadowsocks nodes available")
            await ws.close()
            return ws

        # Parse pool host and port
        pool_host, pool_port_str = PROXY_TARGET.split(':')
        pool_port = int(pool_port_str)

        # Connect through Shadowsocks
        ss_conn = ShadowsocksConnection(ss_node)
        if not await ss_conn.connect():
            logger.error("Failed to establish Shadowsocks connection")
            await ws.close()
            return ws

        # Now we need to tell the Shadowsocks node where to connect to
        # In Shadowsocks protocol, this is handled automatically
        # For simplicity, we'll assume the node is configured to forward to our target
        # In reality, you'd need to send the destination address through the encrypted channel

        # Forward data between WebSocket and Shadowsocks
        async def ws_to_ss():
            try:
                async for msg in ws:
                    if msg.type == web.WSMsgType.BINARY:
                        await ss_conn.send(msg.data)
                    elif msg.type == web.WSMsgType.TEXT:
                        await ss_conn.send(msg.data.encode())
            except Exception as e:
                logger.debug(f"ws_to_ss error: {e}")
            finally:
                await ss_conn.close()

        async def ss_to_ws():
            try:
                while True:
                    data = await ss_conn.receive(4096)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except Exception as e:
                logger.debug(f"ss_to_ws error: {e}")
            finally:
                await ws.close()

        # Run both forwarding tasks concurrently
        task1 = asyncio.create_task(ws_to_ss())
        task2 = asyncio.create_task(ss_to_ws())

        done, pending = await asyncio.wait(
            [task1, task2],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

        active_connections -= 1
        logger.info(f"Client disconnected: {client} | Active: {active_connections}")

    return ws

# ---------- Health check endpoint ----------
async def health_check(request):
    return web.json_response({
        "status": "healthy",
        "active_connections": active_connections,
        "nodes_available": len(node_manager.nodes),
        "uptime": time.time() - start_time,
        "version": "2.3.1"
    })

# ---------- Fake API endpoints ----------
async def root_handler(request):
    return web.Response(text=FAKE_SITE, content_type='text/html')

async def api_status(request):
    return web.json_response({
        "service": "push-notification",
        "version": "2.3.1",
        "ws_endpoint": "/api/v1/push"
    })

# ---------- Background node refresher ----------
async def node_refresher():
    """Periodically refresh Shadowsocks nodes"""
    while True:
        await node_manager.refresh_nodes_if_needed()
        await asyncio.sleep(300)  # Check every 5 minutes

# ---------- Main ----------
start_time = time.time()

async def main():
    # Initial node fetch
    await node_manager.fetch_nodes()
    
    # Start background refresher
    asyncio.create_task(node_refresher())
    
    app = web.Application()
    # Fake website to look legitimate
    app.router.add_get('/', root_handler)
    app.router.add_get('/api/status', api_status)
    app.router.add_get('/health', health_check)
    # Real WebSocket endpoint
    app.router.add_get('/api/v1/push', websocket_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    logger.info(f"Service running on port {PORT}")
    logger.info(f"Fake site: http://localhost:{PORT}/")
    logger.info(f"WebSocket endpoint: ws://localhost:{PORT}/api/v1/push")
    logger.info(f"Shadowsocks enabled: {SHADOWSOCKS_ENABLED}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
