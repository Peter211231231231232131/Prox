#!/usr/bin/env python3
import asyncio
import logging
import os
import random
import time
import aiohttp
from urllib.parse import parse_qs

from aiohttp import web
import socks

# ---------- Configuration ----------
PROXY_TARGET = os.environ.get('PROXY_TARGET', 'pool.supportxmr.com:5555')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')
PORT = int(os.environ.get('PORT', 10000))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', 500))
PROXY_TIMEOUT = 5          # seconds per proxy test
HEALTHY_POOL_SIZE = 10     # keep top 10 fastest proxies
BATCH_SIZE = 50            # test 50 proxies at a time
REFRESH_INTERVAL = 300     # refresh every 5 minutes

# SOCKS5 proxy sources
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt"
]

# Fake site
FAKE_SITE = """
<!DOCTYPE html>
<html>
<head><title>Push Notification API</title></head>
<body>
<h1>Push Notification Service v3.2</h1>
<p>WebSocket endpoint: /api/v1/push</p>
<p>Status: <span style="color:green">operational</span></p>
</body>
</html>
"""

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('smart-proxy')

# ---------- Proxy Manager with Healthy Pool ----------
class SmartProxyManager:
    def __init__(self):
        self.candidate_proxies = []          # all proxies that passed basic TCP test
        self.healthy_pool = []                # list of (host, port, latency)
        self.last_refresh = 0
        self.lock = asyncio.Lock()
        self.test_semaphore = asyncio.Semaphore(50)  # limit concurrency

    def _parse_proxy(self, line):
        """Convert 'host:port' to (host, port) or None."""
        line = line.strip()
        if not line or ':' not in line:
            return None
        parts = line.split(':')
        if len(parts) != 2:
            return None
        host, port_str = parts
        try:
            port = int(port_str)
        except ValueError:
            return None
        # Only ports 1024-65535 (avoid weird system ports)
        if port < 1024 or port > 65535:
            return None
        return (host, port)

    async def fetch_proxies(self):
        """Fetch and parse proxies, do quick TCP test to build candidate list."""
        raw_proxies = set()
        for source in PROXY_SOURCES:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(source, timeout=10) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            for line in text.splitlines():
                                p = self._parse_proxy(line)
                                if p:
                                    raw_proxies.add(p)
                            logger.info(f"Fetched {len(raw_proxies)} raw from {source}")
            except Exception as e:
                logger.error(f"Fetch error {source}: {e}")

        # Quick TCP test to filter dead ones
        logger.info(f"Testing {len(raw_proxies)} raw proxies...")
        tasks = [self._quick_test(p) for p in raw_proxies]
        results = await asyncio.gather(*tasks)
        good = [p for p, ok in zip(raw_proxies, results) if ok]
        logger.info(f"Found {len(good)} alive proxies after TCP test")

        async with self.lock:
            self.candidate_proxies = good
            # If we have no healthy pool yet, trigger an immediate batch test
            if not self.healthy_pool and self.candidate_proxies:
                asyncio.create_task(self._test_batch_async())

    async def _quick_test(self, proxy):
        """Simple TCP connect test."""
        host, port = proxy
        try:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=3
            )
            return True
        except:
            return False

    async def _test_proxy_socks5(self, proxy):
        """Perform full SOCKS5 handshake and measure latency."""
        host, port = proxy
        start = time.time()
        try:
            # Create a SOCKS5 socket and attempt a connection to a known host (e.g., pool)
            # We'll just try to connect to the pool itself through the proxy.
            sock = socks.socksocket()
            sock.set_proxy(socks.SOCKS5, host, port, rdns=True)
            sock.settimeout(PROXY_TIMEOUT)
            # Connect to pool (this is the real test)
            pool_host, pool_port_str = PROXY_TARGET.split(':')
            pool_port = int(pool_port_str)
            await asyncio.get_event_loop().run_in_executor(
                None, sock.connect, (pool_host, pool_port)
            )
            sock.close()
            latency = time.time() - start
            return (host, port, latency)
        except Exception as e:
            logger.debug(f"Proxy {host}:{port} SOCKS5 test failed: {e}")
            return None

    async def _test_batch_async(self):
        """Test a batch of candidate proxies concurrently, update healthy pool."""
        async with self.lock:
            if not self.candidate_proxies:
                logger.warning("No candidates to test")
                return
            # Take a batch (shuffle to get random selection)
            batch = random.sample(
                self.candidate_proxies,
                min(BATCH_SIZE, len(self.candidate_proxies))
            )

        # Test all in batch concurrently
        tasks = [self._test_proxy_socks5(p) for p in batch]
        results = await asyncio.gather(*tasks)

        # Filter successful ones with latency
        successful = [r for r in results if r is not None]
        if not successful:
            logger.info("No working proxies in this batch")
            return

        # Sort by latency
        successful.sort(key=lambda x: x[2])
        top = successful[:HEALTHY_POOL_SIZE]

        async with self.lock:
            self.healthy_pool = top
            logger.info(f"Healthy pool updated with {len(top)} proxies, fastest: {top[0][2]:.3f}s")

    async def maintain_healthy_pool(self):
        """Background task: periodically refresh the healthy pool."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            logger.info("Starting scheduled proxy batch test...")
            await self._test_batch_async()

    async def get_fastest_proxy(self):
        """Return the fastest proxy from healthy pool."""
        async with self.lock:
            if self.healthy_pool:
                # Return the fastest (first) and rotate it to end to spread load
                best = self.healthy_pool.pop(0)
                self.healthy_pool.append(best)
                return best[:2]  # (host, port)
        # If pool empty, try one immediate batch test
        logger.warning("Healthy pool empty, triggering emergency batch test")
        await self._test_batch_async()
        async with self.lock:
            if self.healthy_pool:
                best = self.healthy_pool.pop(0)
                self.healthy_pool.append(best)
                return best[:2]
        return None

# Initialize manager
proxy_manager = SmartProxyManager()

# ---------- Connection tracking ----------
active_connections = 0
semaphore = asyncio.Semaphore(MAX_CONNECTIONS)

# ---------- WebSocket handler ----------
async def websocket_handler(request):
    global active_connections
    ws = web.WebSocketResponse()
    await ws.prepare(request)

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

        # Get a fast proxy
        proxy = await proxy_manager.get_fastest_proxy()
        if not proxy:
            logger.error("No proxy available")
            await ws.close(code=1011, message='No proxy')
            active_connections -= 1
            return ws

        proxy_host, proxy_port = proxy
        logger.info(f"Using proxy {proxy_host}:{proxy_port} for {client}")

        pool_host, pool_port_str = PROXY_TARGET.split(':')
        pool_port = int(pool_port_str)

        try:
            # Create SOCKS5 connection to pool
            sock = socks.socksocket()
            sock.set_proxy(socks.SOCKS5, proxy_host, proxy_port, rdns=True)
            sock.settimeout(30)
            await asyncio.get_event_loop().run_in_executor(
                None, sock.connect, (pool_host, pool_port)
            )
            reader, writer = await asyncio.open_connection(sock=sock)
            logger.info(f"Connected to pool via proxy {proxy_host}:{proxy_port}")
        except Exception as e:
            logger.error(f"Connection failed via {proxy_host}:{proxy_port}: {e}")
            await ws.close(code=1011, message='Connection failed')
            active_connections -= 1
            return ws

        # Forward data
        async def ws_to_pool():
            try:
                async for msg in ws:
                    if msg.type == web.WSMsgType.BINARY:
                        writer.write(msg.data)
                    elif msg.type == web.WSMsgType.TEXT:
                        writer.write(msg.data.encode())
                    await writer.drain()
            except:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        async def pool_to_ws():
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except:
                pass
            finally:
                await ws.close()

        task1 = asyncio.create_task(ws_to_pool())
        task2 = asyncio.create_task(pool_to_ws())

        done, pending = await asyncio.wait(
            [task1, task2],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

        active_connections -= 1
        logger.info(f"Client disconnected: {client} | Active: {active_connections}")
        return ws

# ---------- Health and fake endpoints ----------
async def health_check(request):
    async with proxy_manager.lock:
        healthy_count = len(proxy_manager.healthy_pool)
        candidate_count = len(proxy_manager.candidate_proxies)
    return web.json_response({
        "status": "healthy",
        "active_connections": active_connections,
        "healthy_proxies": healthy_count,
        "candidate_proxies": candidate_count,
        "uptime": time.time() - start_time,
        "version": "3.2.0"
    })

async def root_handler(request):
    return web.Response(text=FAKE_SITE, content_type='text/html')

async def api_status(request):
    return web.json_response({
        "service": "push-notification",
        "version": "3.2.0",
        "ws_endpoint": "/api/v1/push"
    })

# ---------- Main ----------
start_time = time.time()

async def main():
    # Initial proxy fetch
    await proxy_manager.fetch_proxies()
    proxy_manager.last_refresh = time.time()

    # Start background maintainer
    asyncio.create_task(proxy_manager.maintain_healthy_pool())

    app = web.Application()
    app.router.add_get('/', root_handler)
    app.router.add_get('/api/status', api_status)
    app.router.add_get('/health', health_check)
    app.router.add_get('/api/v1/push', websocket_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    logger.info(f"Service running on port {PORT}")
    logger.info(f"Fake site: http://localhost:{PORT}/")
    logger.info(f"WebSocket endpoint: ws://localhost:{PORT}/api/v1/push")
    logger.info(f"Candidate proxies: {len(proxy_manager.candidate_proxies)}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    import socket  # needed for quick test
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")