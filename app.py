#!/usr/bin/env python3
import asyncio
import logging
import os
import random
import time
import aiohttp
import json
from urllib.parse import urlparse, parse_qs

from aiohttp import web
from aiohttp_socks import ProxyConnector, ProxyType

# ---------- Configuration ----------
PROXY_TARGET = os.environ.get('PROXY_TARGET', 'pool.supportxmr.com:5555')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')
PORT = int(os.environ.get('PORT', 10000))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', 500))

# SOCKS5 proxy sources (updated frequently)
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"
]

PROXY_REFRESH_INTERVAL = 600  # Refresh every 10 minutes

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
logger = logging.getLogger('socks5-proxy')

# ---------- SOCKS5 Proxy Manager ----------
class SOCKS5Manager:
    def __init__(self):
        self.proxies = []
        self.current_proxy_index = 0
        self.last_refresh = 0
        
    async def fetch_proxies(self):
        """Fetch SOCKS5 proxies from multiple sources"""
        all_proxies = []
        
        for source in PROXY_SOURCES:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(source, timeout=10) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            # Each line is "ip:port"
                            proxies = [line.strip() for line in text.split('\n') 
                                     if line.strip() and ':' in line.strip()]
                            all_proxies.extend(proxies)
                            logger.info(f"Fetched {len(proxies)} proxies from {source}")
            except Exception as e:
                logger.error(f"Failed to fetch from {source}: {e}")
        
        if all_proxies:
            # Remove duplicates and shuffle
            self.proxies = list(set(all_proxies))
            random.shuffle(self.proxies)
            logger.info(f"Total unique proxies available: {len(self.proxies)}")
            
            # Test a few to make sure they're alive
            await self.test_proxies()
        else:
            logger.warning("No proxies fetched, using fallback list")
            self.proxies = self.get_fallback_proxies()
    
    async def test_proxies(self):
        """Quickly test a sample of proxies to find working ones"""
        working = []
        sample = self.proxies[:20]  # Test first 20
        
        for proxy in sample:
            if await self.test_proxy(proxy):
                working.append(proxy)
        
        if working:
            logger.info(f"Found {len(working)} working proxies in sample")
            # Prioritize working ones
            self.proxies = working + [p for p in self.proxies if p not in working]
    
    async def test_proxy(self, proxy_str):
        """Test if a proxy is reachable"""
        try:
            host, port = proxy_str.split(':')
            port = int(port)
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=host,
                port=port,
                rdns=True
            )
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get('http://www.google.com', timeout=5) as resp:
                    return resp.status == 200
        except:
            return False
    
    def get_fallback_proxies(self):
        """Fallback hardcoded proxies (last resort)"""
        return [
            "45.155.68.129:8133",
            "185.217.131.117:1080",
            "51.158.68.133:16379"
        ]
    
    async def refresh_if_needed(self):
        """Refresh proxy list if interval has passed"""
        if time.time() - self.last_refresh > PROXY_REFRESH_INTERVAL:
            await self.fetch_proxies()
            self.last_refresh = time.time()
    
    def get_next_proxy(self):
        """Get next proxy in rotation (round-robin)"""
        if not self.proxies:
            return None
        proxy = self.proxies[self.current_proxy_index % len(self.proxies)]
        self.current_proxy_index += 1
        return proxy

# Initialize proxy manager
proxy_manager = SOCKS5Manager()

# ---------- Connection tracking ----------
active_connections = 0
semaphore = asyncio.Semaphore(MAX_CONNECTIONS)

# ---------- WebSocket handler (with SOCKS5 routing) ----------
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

        # Get a SOCKS5 proxy
        proxy_str = proxy_manager.get_next_proxy()
        if not proxy_str:
            logger.error("No SOCKS5 proxies available")
            await ws.close(code=1011, message='No proxy available')
            return ws

        # Parse proxy
        proxy_host, proxy_port = proxy_str.split(':')
        proxy_port = int(proxy_port)
        
        # Parse pool host and port
        pool_host, pool_port_str = PROXY_TARGET.split(':')
        pool_port = int(pool_port_str)

        # Create SOCKS5 connector
        connector = ProxyConnector(
            proxy_type=ProxyType.SOCKS5,
            host=proxy_host,
            port=proxy_port,
            rdns=True
        )

        try:
            # Connect to pool through SOCKS5 proxy
            logger.info(f"Connecting via SOCKS5 proxy {proxy_host}:{proxy_port}")
            
            # Create TCP connection through SOCKS5
            from aiohttp_socks.utils import open_connection
            pool_reader, pool_writer = await open_connection(
                proxy_type=ProxyType.SOCKS5,
                proxy_host=proxy_host,
                proxy_port=proxy_port,
                host=pool_host,
                port=pool_port,
                rdns=True
            )
            logger.info(f"Connected to pool {PROXY_TARGET} via proxy for {client}")

            # Forward data between WebSocket and TCP socket
            async def ws_to_pool():
                try:
                    async for msg in ws:
                        if msg.type == web.WSMsgType.BINARY:
                            pool_writer.write(msg.data)
                        elif msg.type == web.WSMsgType.TEXT:
                            pool_writer.write(msg.data.encode())
                        await pool_writer.drain()
                except Exception as e:
                    logger.debug(f"ws_to_pool error: {e}")
                finally:
                    pool_writer.close()
                    await pool_writer.wait_closed()

            async def pool_to_ws():
                try:
                    while True:
                        data = await pool_reader.read(4096)
                        if not data:
                            break
                        await ws.send_bytes(data)
                except Exception as e:
                    logger.debug(f"pool_to_ws error: {e}")
                finally:
                    await ws.close()

            # Run both forwarding tasks concurrently
            task1 = asyncio.create_task(ws_to_pool())
            task2 = asyncio.create_task(pool_to_ws())

            done, pending = await asyncio.wait(
                [task1, task2],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

        except Exception as e:
            logger.error(f"Connection failed via proxy {proxy_str}: {e}")
            await ws.close(code=1011, message='Connection failed')
        finally:
            active_connections -= 1
            logger.info(f"Client disconnected: {client} | Active: {active_connections}")

    return ws

# ---------- Health check endpoint ----------
async def health_check(request):
    return web.json_response({
        "status": "healthy",
        "active_connections": active_connections,
        "proxies_available": len(proxy_manager.proxies),
        "uptime": time.time() - start_time,
        "version": "3.0.0"
    })

# ---------- Fake API endpoints ----------
async def root_handler(request):
    return web.Response(text=FAKE_SITE, content_type='text/html')

async def api_status(request):
    return web.json_response({
        "service": "push-notification",
        "version": "3.0.0",
        "ws_endpoint": "/api/v1/push"
    })

# ---------- Background proxy refresher ----------
async def proxy_refresher():
    """Periodically refresh SOCKS5 proxies"""
    while True:
        await proxy_manager.refresh_if_needed()
        await asyncio.sleep(60)  # Check every minute

# ---------- Main ----------
start_time = time.time()

async def main():
    # Initial proxy fetch
    await proxy_manager.fetch_proxies()
    proxy_manager.last_refresh = time.time()
    
    # Start background refresher
    asyncio.create_task(proxy_refresher())
    
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
    logger.info(f"SOCKS5 proxies loaded: {len(proxy_manager.proxies)}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")