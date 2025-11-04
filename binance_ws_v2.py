import asyncio
import json
import threading
import websockets
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager

active_symbols = set()
reconnect_event = asyncio.Event()
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"

# --- Background Binance WebSocket loop ---
async def binance_stream():
    global active_symbols
    while True:
        if not active_symbols:
            await asyncio.sleep(2)
            continue

        streams = [f"{s.lower()}usdt@ticker" for s in active_symbols]
        stream_url = f"{BINANCE_WS_URL}?streams={'/'.join(streams)}"
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("URL:", stream_url)
        print(f"Connecting to Binance for streams: {streams}")
        
        try:
            async with websockets.connect(stream_url) as websocket:
                print("✅ Connected to Binance WebSocket")
                
                # Create tasks for receiving messages and waiting for reconnect signal
                receive_task = asyncio.create_task(receive_messages(websocket))
                reconnect_task = asyncio.create_task(reconnect_event.wait())
                
                # Wait for either task to complete
                done, pending = await asyncio.wait(
                    [receive_task, reconnect_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # Cancel the pending task
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                # If reconnect was triggered, clear the event and loop will reconnect
                if reconnect_task in done:
                    print("🔄 Reconnecting due to symbol change...")
                    reconnect_event.clear()
                else:
                    # receive_task completed (likely an error), will reconnect anyway
                    print("⚠️ Connection lost, reconnecting...")
                    
        except Exception as e:
            print(f"Binance WS error: {e}")
            await asyncio.sleep(5)

async def receive_messages(ws):
    """Separate function to receive messages from WebSocket"""
    try:
        async for msg in ws:
            try:
                data = json.loads(msg)
                stream_data = data.get("data", {})
                symbol = stream_data.get("s")
                price = stream_data.get("c")
                print(f"[BINANCE] {symbol}: {price}")
            except Exception as e:
                print(f"Error processing message: {e}")
    except Exception as e:
        print(f"Error receiving messages: {e}")
        raise

# --- Lifespan context manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔄 Starting Binance streamer...")
    loop = asyncio.new_event_loop()
    
    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(binance_stream())
    
    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    
    # Store the loop in app state so we can access it from endpoints
    app.state.binance_loop = loop
    
    yield
    print("🛑 Stopping Binance streamer...")

# --- FastAPI app ---
app = FastAPI(lifespan=lifespan)

@app.post("/add_symbol")
async def add_symbol(request: Request):
    payload = await request.json()
    symbol = payload.get("symbol", "").upper()
    if not symbol:
        return {"error": "No symbol provided"}

    if symbol in active_symbols:
        return {"message": f"{symbol} already tracked"}

    active_symbols.add(symbol)
    print(f"✅ Added new symbol: {symbol}")
    print(f"Current tracked symbols: {active_symbols}")
    
    # Trigger reconnection in the binance loop
    asyncio.run_coroutine_threadsafe(trigger_reconnect(), app.state.binance_loop)
    
    return {"message": f"Tracking {symbol}"}

async def trigger_reconnect():
    """Helper to set the reconnect event"""
    reconnect_event.set()

@app.get("/")
def root():
    return {"status": "Binance streamer running", "symbols": list(active_symbols)}