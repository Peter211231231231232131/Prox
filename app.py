#!/usr/bin/env python3
"""
Mining Pool Proxy for Render
Forward WebSocket connections from miners to actual pool with optional auth.
"""

import asyncio
import aiohttp
from aiohttp import web
import websockets
import logging
import os
import ssl
import json
from urllib.parse import urlparse, parse_qs
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('mining-proxy')

# Environment variables with defaults
PROXY_TARGET = os.environ.get('PROXY_TARGET', 'pool.supportxmr.com:5555')
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', None)  # If set, require token
LISTEN_PORT = int(os.environ.get('PORT', 8765))  # Render sets PORT
USE_TLS = os.environ.get('USE_TLS', 'false').lower() == 'true'
TLS_CERT = os.environ.get('TLS_CERT', '/etc/ssl/certs/cert.pem')
TLS_KEY = os.environ.get('TLS_KEY', '/etc/ssl/private/key.pem')
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', 1000))
CONNECTION_TIMEOUT = int(os.environ.get('CONNECTION_TIMEOUT', 600))  # 10 minutes

# Connection counter
active_connections = 0
connection_semaphore = asyncio.Semaphore(MAX_CONNECTIONS)

async def forward_messages(reader, writer, direction):
    """Bidirectional message forwarding with logging."""
    try:
        async for message in reader:
            logger.debug(f"{direction}: {message[:100]}...")
            await writer.send(message)
    except websockets.exceptions.ConnectionClosed:
        logger.info(f"{direction} connection closed")
    except Exception as e:
        logger.error(f"Error in {direction}: {e}")

async def handle_miner(websocket, path):
    """Handle incoming miner connection."""
    global active_connections
    
    # Rate limiting via semaphore
    async with connection_semaphore:
        active_connections += 1
        client_ip = websocket.remote_address[0]
        logger.info(f"New connection from {client_ip}, active: {active_connections}")
        
        # Optional authentication via query string or headers
        if ACCESS_TOKEN:
            query = parse_qs(urlparse(path).query)
            token = query.get('token', [None])[0]
            auth_header = websocket.request_headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]
            
            if token != ACCESS_TOKEN:
                logger.warning(f"Authentication failed from {client_ip}")
                await websocket.close(code=1008, reason='Unauthorized')
                active_connections -= 1
                return
        
        # Determine if target is TLS
        target_uri = f"ws://{PROXY_TARGET}"
        if PROXY_TARGET.endswith(':443') or 'wss://' in PROXY_TARGET:
            target_uri = f"wss://{PROXY_TARGET}"
        elif USE_TLS:
            target_uri = f"wss://{PROXY_TARGET}"
        else:
            target_uri = f"ws://{PROXY_TARGET}"
        
        try:
            # Connect to actual pool with timeout
            async with asyncio.timeout(CONNECTION_TIMEOUT):
                async with websockets.connect(
                    target_uri,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5
                ) as pool_ws:
                    logger.info(f"Connected to pool {PROXY_TARGET} for {client_ip}")
                    
                    # Bidirectional forwarding
                    task1 = asyncio.create_task(forward_messages(websocket, pool_ws, "Miner->Pool"))
                    task2 = asyncio.create_task(forward_messages(pool_ws, websocket, "Pool->Miner"))
                    
                    # Wait for either task to complete (connection closed)
                    done, pending = await asyncio.wait(
                        [task1, task2],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    # Cancel the other task
                    for task in pending:
                        task.cancel()
                    
        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to pool for {client_ip}")
            await websocket.close(code=1011, reason='Pool timeout')
        except websockets.exceptions.WebSocketException as e:
            logger.error(f"WebSocket error for {client_ip}: {e}")
            await websocket.close(code=1011, reason='Pool error')
        except Exception as e:
            logger.error(f"Unexpected error for {client_ip}: {e}")
            await websocket.close(code=1011, reason='Internal error')
        finally:
            active_connections -= 1
            logger.info(f"Connection closed from {client_ip}, active: {active_connections}")

async def health_check(request):
    """Simple HTTP health check endpoint."""
    return web.Response(text=f"OK - Active connections: {active_connections}")

async def main():
    """Start the WebSocket server."""
    # Configure SSL if needed
    ssl_context = None
    if USE_TLS and os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(TLS_CERT, TLS_KEY)
        logger.info("TLS enabled")
    
    # Start server
    server = await websockets.serve(
        handle_miner,
        "0.0.0.0",
        LISTEN_PORT,
        ssl=ssl_context,
        ping_interval=30,
        ping_timeout=10,
        max_size=2**20  # 1MB max message
    )
    
    logger.info(f"Proxy listening on port {LISTEN_PORT}" + (" with TLS" if ssl_context else ""))
    
    # Also run a tiny HTTP server for health checks
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Health check server running on port 8080")
    
    # Keep running
    await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
