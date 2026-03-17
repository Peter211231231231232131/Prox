#!/usr/env/bin python3
import asyncio
import logging
import os
import random
import time
from urllib.parse import urlparse, parse_qs

from aiohttp import web
import websockets

# ---------- Configuration ----------
PROXY_TARGET = os.environ.get('PROXY_TARGET', 'pool.supportxmr.com:5555')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')          # Optional but recommended
PORT = int(os.environ.get('PORT', 10000))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', 500))

# Fake site content (looks like a real API)
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

# ---------- Connection tracking ----------
active_connections = 0
semaphore = asyncio.Semaphore(MAX_CONNECTIONS)

# ---------- Fake heartbeat generator ----------
async def fake_heartbeat(ws, pool_ws):
    """Send occasional fake messages to the miner to mimic a real service."""
    while not ws.closed and not pool_ws.closed:
        await asyncio.sleep(random.randint(30, 90))
        try:
            # Send a fake notification (e.g., JSON with keepalive)
            fake_msg = '{"type":"ping","timestamp":%d}' % int(time.time())
            await ws.send_str(fake_msg)
            logger.debug("Sent fake heartbeat to miner")
        except:
            break

# ---------- WebSocket handler (disguised as /api/v1/push) ----------
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

        try:
            # Connect to the actual mining pool (with a small random delay to avoid patterns)
            await asyncio.sleep(random.uniform(0.1, 0.5))
            pool_ws = await websockets.connect(f"ws://{PROXY_TARGET}")
        except Exception as e:
            logger.error(f"Pool connection failed: {e}")
            await ws.close()
            return ws

        # Start fake heartbeat task
        heartbeat_task = asyncio.create_task(fake_heartbeat(ws, pool_ws))

        # Bidirectional forwarding
        async def forward(src, dst, direction):
            try:
                async for msg in src:
                    if isinstance(msg, bytes):
                        await dst.send_bytes(msg)
                    else:
                        await dst.send_str(msg)
                    # Small random delay to mimic human traffic
                    await asyncio.sleep(random.uniform(0.01, 0.05))
            except:
                pass

        task1 = asyncio.create_task(forward(ws, pool_ws, "→ pool"))
        task2 = asyncio.create_task(forward(pool_ws, ws, "← client"))

        # Wait for one to finish (connection closed)
        done, pending = await asyncio.wait(
            [task1, task2, heartbeat_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

        await pool_ws.close()
        active_connections -= 1
        logger.info(f"Client disconnected: {client} | Active: {active_connections}")

    return ws

# ---------- Health check (returns JSON, like a real API) ----------
async def health_check(request):
    return web.json_response({
        "status": "healthy",
        "active_connections": active_connections,
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

# ---------- Main ----------
start_time = time.time()

async def main():
    app = web.Application()
    # Fake website to look legitimate
    app.router.add_get('/', root_handler)
    app.router.add_get('/api/status', api_status)
    app.router.add_get('/health', health_check)
    # Real WebSocket endpoint hidden under a generic path
    app.router.add_get('/api/v1/push', websocket_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    logger.info(f"Service running on port {PORT}")
    logger.info(f"Fake site: http://localhost:{PORT}/")
    logger.info(f"WebSocket endpoint: ws://localhost:{PORT}/api/v1/push")

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
