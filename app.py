#!/usr/bin/env python3
import asyncio
import logging
import os
import ssl
from urllib.parse import urlparse, parse_qs

from aiohttp import web
import websockets

# ---------- Configuration ----------
PROXY_TARGET = os.environ.get('PROXY_TARGET', 'pool.supportxmr.com:5555')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')          # Optional
PORT = int(os.environ.get('PORT', 10000))              # Render sets this
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', 500))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('mining-proxy')

# ---------- Connection tracking ----------
active_connections = 0
semaphore = asyncio.Semaphore(MAX_CONNECTIONS)

# ---------- WebSocket handler ----------
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
        logger.info(f"Connect: {client} | Active: {active_connections}")

        try:
            # Connect to the actual mining pool
            pool_ws = await websockets.connect(f"ws://{PROXY_TARGET}")
        except Exception as e:
            logger.error(f"Failed to connect to pool {PROXY_TARGET}: {e}")
            await ws.close()
            return ws

        # Bidirectional forwarding
        async def forward(src, dst):
            try:
                async for msg in src:
                    if isinstance(msg, bytes):
                        await dst.send_bytes(msg)
                    else:
                        await dst.send_str(msg)
            except:
                pass

        # Create two forwarding tasks
        task1 = asyncio.create_task(forward(ws, pool_ws))
        task2 = asyncio.create_task(forward(pool_ws, ws))

        # Wait for one to finish (connection closed)
        done, pending = await asyncio.wait(
            [task1, task2],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

        await pool_ws.close()
        active_connections -= 1
        logger.info(f"Disconnect: {client} | Active: {active_connections}")

    return ws

# ---------- Health check endpoint ----------
async def health_check(request):
    return web.Response(text=f"OK - Active connections: {active_connections}")

# ---------- Main ----------
async def main():
    app = web.Application()
    app.router.add_get('/', websocket_handler)      # WebSocket upgrade at root
    app.router.add_get('/health', health_check)     # Health check endpoint

    # Optional: handle both / and /ws for flexibility
    app.router.add_get('/ws', websocket_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    logger.info(f"Proxy listening on port {PORT} (WebSocket + HTTP)")
    logger.info(f"Forwarding to pool: {PROXY_TARGET}")

    # Keep running
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
