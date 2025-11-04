import asyncio
import json
import websockets
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager

# --- OKX Public WebSocket Endpoint ---
OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

# --- Globals ---
active_symbols = set()  # e.g. {"SOL-USDT", "BTC-USDT"}
latest_prices = {}      # symbol -> price
reconnect_event = None  # Will be initialized in lifespan
stream_task = None      # Reference to the background task


# --- Background OKX WebSocket Stream ---
async def okx_stream():
    """Main OKX WebSocket streaming loop"""
    global active_symbols, latest_prices, reconnect_event

    while True:
        if not active_symbols:
            await asyncio.sleep(2)
            continue

        print(f"Connecting to OKX for symbols: {list(active_symbols)}")

        try:
            async with websockets.connect(OKX_WS_URL) as ws:
                print("✅ Connected to OKX WebSocket")

                # Subscribe to tickers for all active symbols
                sub_msg = {
                    "op": "subscribe",
                    "args": [{"channel": "ticker", "instId": sym} for sym in active_symbols]
                }
                await ws.send(json.dumps(sub_msg))
                print(f"📡 Subscribed to tickers: {list(active_symbols)}")

                # Create tasks
                receive_task = asyncio.create_task(receive_okx_messages(ws))
                reconnect_task = asyncio.create_task(reconnect_event.wait())

                done, pending = await asyncio.wait(
                    [receive_task, reconnect_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                if reconnect_task in done:
                    print("🔄 Reconnecting due to symbol list change...")
                    reconnect_event.clear()
                else:
                    print("⚠️ Connection lost, retrying...")

        except Exception as e:
            print(f"OKX WebSocket error: {e}")
            await asyncio.sleep(5)


# --- Message Receiver ---
async def receive_okx_messages(ws):
    """Handles incoming OKX WebSocket messages"""
    try:
        async for msg in ws:
            try:
                data = json.loads(msg)

                # Subscription confirmation
                if data.get("event") == "subscribe":
                    print(f"✅ Subscribed to: {data.get('arg', {}).get('instId')}")
                    continue

                # Price update
                if "data" in data:
                    for item in data["data"]:
                        symbol = item.get("instId")
                        last_price = float(item.get("last", 0))
                        if symbol:
                            latest_prices[symbol] = last_price
                            print(f"💰 {symbol}: {last_price}")
            except Exception as e:
                print(f"Error processing message: {e}")
                print(f"Raw message: {msg[:300]}")
    except Exception as e:
        print(f"Error receiving OKX messages: {e}")
        raise


# --- Lifespan Context Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global reconnect_event, stream_task
    
    print("🔄 Starting OKX streamer...")
    
    # Initialize reconnect event in the current event loop
    reconnect_event = asyncio.Event()
    
    # Start the OKX stream as a background task in the same event loop
    stream_task = asyncio.create_task(okx_stream())
    
    yield
    
    print("🛑 Stopping OKX streamer...")
    if stream_task:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass


# --- FastAPI App ---
app = FastAPI(lifespan=lifespan)


@app.post("/add_symbol")
async def add_symbol(request: Request):
    """Add a trading pair to track (e.g., SOL-USDT, BTC-USDT)"""
    global reconnect_event
    
    payload = await request.json()
    symbol = payload.get("symbol", "").strip().upper()
    if not symbol:
        return {"error": "No symbol provided"}
    
    # Add -USDT suffix if not present
    if "-" not in symbol:
        symbol = symbol + "-USDC"

    if symbol in active_symbols:
        return {"message": f"{symbol} already being tracked", "price": latest_prices.get(symbol)}

    active_symbols.add(symbol)
    print(f"✅ Added new symbol: {symbol}")
    print(f"Current tracked symbols: {active_symbols}")

    # Trigger reconnect in the same event loop
    reconnect_event.set()

    return {"message": f"Tracking {symbol}"}


@app.get("/")
def root():
    return {
        "status": "OKX streamer running",
        "symbols": list(active_symbols),
        "prices": latest_prices
    }