from fastapi import FastAPI, BackgroundTasks
from telethon import TelegramClient, events
import asyncio
import httpx
import os
from contextlib import asynccontextmanager
import re
from mint_address import get_token_from_dexscreener

# Configuration
API_ID = '23265688'
API_HASH = '796729041139bc65d33b024ce04f6b5f'
PHONE = '+919964063864'
BINANCE_SERVER_URL = os.getenv("BINANCE_SERVER_URL", "http://localhost:8001/add_symbol")
active_symbols = set()

# Telegram client
client = TelegramClient('session_name', API_ID, API_HASH)

def format_message(self, event):
    """Format message data"""
    msg = event.message
    return {
        'id': msg.id,
        'date': msg.date.isoformat(),
        'text': msg.text or '',
        'sender_id': msg.sender_id,
        'views': msg.views,
        'forwards': msg.forwards,
        'has_media': msg.media is not None,
        'media_type': type(msg.media).__name__ if msg.media else None,
        'reply_to': msg.reply_to_msg_id
    }



async def start_telegram_listener():
    """Start listening to Telegram messages in background"""
    await client.start(phone=PHONE)
    print("Telegram client started and listening...")
    
    @client.on(events.NewMessage)
    async def handle_new_message(event):
        message_text = event.message.message
        global active_symbols
        # print(f"Received message: {message_text}")
        msg_data = format_message(None, event)
        
        # Extract coin symbol (basic example - you may need more sophisticated parsing)
        # Assuming format like "BTC" or "BTCUSDT"
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"📩 New Message (ID: {msg_data['id']})")
        print(f"🕒 Time: {msg_data['date']}")
        print(f"📝 Text: {msg_data['text'][:100]}{'...' if len(msg_data['text']) > 100 else ''}")
        match = re.search(r'/\s*(.*?)\*', msg_data['text'])
        if match:
            coin_symbol = match.group(1).upper()
            # mint_address = get_token_from_dexscreener(coin_symbol)
            print(f"🔗 Extracted COIN SYMBOL: {match.group(1)}")
            # print(f"✅ Token Address: {mint_address}")
        
        if coin_symbol and coin_symbol not in active_symbols:
            # mint_address = str(mint_address).upper().strip()
            await send_to_binance_server(coin_symbol)
    
    await client.run_until_disconnected()

# def extract_coin_symbol(message: str) -> str:
#     """Extract coin symbol from message text"""
#     # Simple extraction - customize based on your message format
#     message = message.strip().upper()
    
#     # Check if it's already in USDT format
#     if "USDT" in message:
#         # Extract the part before USDT
#         parts = message.split("USDT")
#         if parts[0]:
#             return f"{parts[0]}USDT"
    
#     # Otherwise, check for common coin symbols
#     common_coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "MATIC", "DOT"]
#     for coin in common_coins:
#         if coin in message:
#             return f"{coin}USDT"
    
#     # Return the first word if nothing matches
#     words = message.split()
#     if words:
#         symbol = words[0].replace("$", "").replace("#", "")
#         if symbol and not symbol.startswith("HTTP"):
#             return f"{symbol}USDT" if not symbol.endswith("USDT") else symbol
    
#     return ""

async def send_to_binance_server(coin_symbol: str):
    """Send POST request to Binance server"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.post(
                BINANCE_SERVER_URL,
                json={"symbol": coin_symbol}
            )
            print(f"Sent {coin_symbol} to Binance server. Status: {response.status_code}")
    except Exception as e:
        print(f"Error sending to Binance server: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start Telegram listener in background
    task = asyncio.create_task(start_telegram_listener())
    yield
    # Shutdown: Cancel the task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await client.disconnect()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "Telegram listener active"}

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "telegram_connected": client.is_connected()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)