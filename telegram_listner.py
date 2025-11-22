from fastapi import FastAPI, BackgroundTasks
from telethon import TelegramClient, events
import asyncio
import httpx
import os
from contextlib import asynccontextmanager
import re
from utils.mint_address import get_token_from_dexscreener

# Configuration
API_ID = '23265688'
API_HASH = '796729041139bc65d33b024ce04f6b5f'
PHONE = '+919964063864'
HELIUS_SERVER_URL = os.getenv("WEBSOCKET_SERVER_URL", "http://localhost:8080/tokens")
active_symbols = set()
rejected_symbols = set()

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
            # check if coin symbol does not exist in active_symbols
            if not(coin_symbol in active_symbols or coin_symbol in rejected_symbols):
                print(f"🔗 Extracted COIN SYMBOL: {match.group(1)}")
  
                response = await get_token_from_dexscreener(coin_symbol)
                mint_address = response.get("address", "")
                symbol = response.get("symbol", "")
                price_usd = response.get("price_usd", "")
                print(f"🔍 DexScreener Result - Mint: {mint_address}, Symbol: {symbol}, Price USD: {price_usd}")
                await send_to_helius_server(mint_address, symbol, price_usd)
                active_symbols.add(coin_symbol)

    await client.run_until_disconnected()

async def send_to_helius_server(mint_address: str, symbol: str, price_usd: str):
    """Send POST request to Helius server"""
    try:
        payload = {"mint_address": mint_address}
        
        if symbol:
            payload["symbol"] = symbol
        
        if price_usd:
            payload["price_usd"] = float(price_usd)
        print(f"Sending payload to Helius server: {payload}")

        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.post(
                HELIUS_SERVER_URL,
                json=payload
            )
            print(f"Sent {symbol} to Binance server. Status: {response.status_code}")
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