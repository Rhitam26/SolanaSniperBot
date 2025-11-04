import asyncio
import json
import threading
import websockets
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
# from solders.pubkey import Pubkey
# from solders.rpc.responses import RpcConfirmedTransactionStatusWithSignature
import base64
import struct

# --- Configuration ---
SOLANA_WS_URL = "wss://api.mainnet-beta.solana.com"
SOLANA_HTTP_URL = "https://api.mainnet-beta.solana.com"
RAYDIUM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
WSOL_MINT = "So11111111111111111111111111111111111111112"  # Wrapped SOL

# --- Globals ---
active_mints = set()
reconnect_event = asyncio.Event()

# --- Helper function to fetch transaction details ---
async def fetch_transaction_details(signature: str):
    """Fetch full transaction details from Solana RPC"""
    import aiohttp
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed"
            }
        ]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SOLANA_HTTP_URL, json=payload) as response:
                data = await response.json()
                print("***** Fetching transaction details for signature:", signature)
                print("Fetched transaction data:", data)
                return data.get("result")
                
    except Exception as e:
        print(f"Error fetching transaction: {e}")
        return None

def parse_swap_data(tx_data, target_mint):
    """Extract swap information from transaction data"""
    if not tx_data or not tx_data.get("meta"):
        return None
    
    meta = tx_data["meta"]
    transaction = tx_data.get("transaction", {})
    message = transaction.get("message", {})
    
    # Get pre and post token balances
    pre_balances = meta.get("preTokenBalances", [])
    post_balances = meta.get("postTokenBalances", [])
    
    # Track changes for target token and WSOL
    target_change = None
    sol_change = None
    user_account = None
    
    # Create balance maps
    pre_map = {b["accountIndex"]: b for b in pre_balances}
    post_map = {b["accountIndex"]: b for b in post_balances}
    
    # Find changes in balances
    all_indices = set(pre_map.keys()) | set(post_map.keys())
    
    for idx in all_indices:
        pre = pre_map.get(idx, {})
        post = post_map.get(idx, {})
        
        mint = pre.get("mint") or post.get("mint")
        owner = pre.get("owner") or post.get("owner")
        
        pre_amount = float(pre.get("uiTokenAmount", {}).get("uiAmount", 0))
        post_amount = float(post.get("uiTokenAmount", {}).get("uiAmount", 0))
        
        change = post_amount - pre_amount
        
        if mint == target_mint and change != 0:
            target_change = change
            user_account = owner
        elif mint == WSOL_MINT and change != 0:
            sol_change = change
            if not user_account:
                user_account = owner
    
    if target_change is not None and sol_change is not None:
        # Determine swap direction
        if target_change > 0 and sol_change < 0:
            swap_type = "BUY"
            token_amount = target_change
            sol_amount = abs(sol_change)
        elif target_change < 0 and sol_change > 0:
            swap_type = "SELL"
            token_amount = abs(target_change)
            sol_amount = sol_change
        else:
            return None
        
        # Calculate price
        price = sol_amount / token_amount if token_amount != 0 else 0
        
        return {
            "type": swap_type,
            "token_amount": token_amount,
            "sol_amount": sol_amount,
            "price": price,
            "user": user_account
        }
    
    return None

# --- Background Solana WebSocket loop ---
async def solana_stream():
    global active_mints
    while True:
        if not active_mints:
            await asyncio.sleep(2)
            continue

        print(f"Connecting to Solana for mints: {list(active_mints)}")

        try:
            async with websockets.connect(SOLANA_WS_URL) as ws:
                print("✅ Connected to Solana WebSocket")

                # Subscribe to Raydium logs for swap events
                raydium_msg = {
                    "jsonrpc": "2.0",
                    "id": "raydium_logs",
                    "method": "logsSubscribe",
                    "params": [
                        {
                            "mentions": [RAYDIUM_PROGRAM_ID]
                        },
                        {
                            "commitment": "confirmed"
                        }
                    ]
                }
                await ws.send(json.dumps(raydium_msg))
                print(f"📡 Subscribed to Raydium swap events for mints: {list(active_mints)}")

                # Wait for messages or reconnect event
                receive_task = asyncio.create_task(receive_solana_messages(ws))
                reconnect_task = asyncio.create_task(reconnect_event.wait())

                done, pending = await asyncio.wait(
                    [receive_task, reconnect_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Cleanup pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                if reconnect_task in done:
                    print("🔄 Reconnecting due to mint list change...")
                    reconnect_event.clear()
                else:
                    print("⚠️ Connection lost, reconnecting...")

        except Exception as e:
            print(f"Solana WS error: {e}")
            await asyncio.sleep(5)

# --- Message receiver ---
async def receive_solana_messages(ws):
    """Handle incoming messages from Solana WebSocket"""
    try:
        async for msg in ws:
            try:
                data = json.loads(msg)
                
                # Handle subscription confirmation
                if "result" in data and "error" not in data:
                    print(f"✅ Subscription confirmed: {data.get('id')}")
                    continue
                
                # Handle log notifications (potential swaps)
                if "params" in data and "result" in data["params"]:
                    result = data["params"]["result"]
                    value = result.get("value", {})
                    # print("Value:", value)
                    
                    if "logs" in value:
                        signature = value.get("signature")
                        logs = value.get("logs", [])
                        
                        # Check if logs indicate a swap
                        has_swap = any("swap" in log.lower() or "ray_log" in log.lower() for log in logs)
                        
                        if has_swap and signature:
                            # Fetch full transaction details
                            tx_data = await fetch_transaction_details(signature)
                            
                    
                            if tx_data:
                                # Check each tracked mint
                                for mint in active_mints:
                                    # swap_info = parse_swap_data(tx_data, mint)
                                    print("Transaction Data:", tx_data)
                                    # if swap_info:
                                    print(f"\n{'🟢' if tx_data['type'] == 'BUY' else '🔴'} [{tx_data['type']}] Signature: {signature}")
                                    print(f"   Token: {mint}")
                                    print(f"   Amount: {tx_data['token_amount']:.4f} tokens")
                                    print(f"   SOL: {tx_data['sol_amount']:.4f} SOL")
                                    print(f"   Price: {tx_data['price']:.8f} SOL/token")
                                    print(f"   User: {tx_data['user']}")
                                    print(f"   View: https://solscan.io/tx/{signature}")
                            
            except Exception as e:
                print(f"Error processing Solana message: {e}")
                
    except Exception as e:
        print(f"Error receiving Solana messages: {e}")
        raise

# --- Lifespan context manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🔄 Starting Solana swap tracker...")
    loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(solana_stream())

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    app.state.solana_loop = loop
    yield
    print("🛑 Stopping Solana swap tracker...")

# --- FastAPI App ---
app = FastAPI(lifespan=lifespan)

@app.post("/add_mint")
async def add_mint(request: Request):
    """Add a new token mint to track"""
    payload = await request.json()
    mint = payload.get("mint", "").strip()
    if not mint:
        return {"error": "No mint provided"}

    if mint in active_mints:
        return {"message": f"{mint} already being tracked"}

    active_mints.add(mint)
    print(f"✅ Added new mint: {mint}")
    print(f"Current tracked mints: {active_mints}")

    # Trigger reconnect in the Solana loop
    asyncio.run_coroutine_threadsafe(trigger_reconnect(), app.state.solana_loop)

    return {"message": f"Tracking swaps for {mint}"}

@app.post("/remove_mint")
async def remove_mint(request: Request):
    """Remove a token mint from tracking"""
    payload = await request.json()
    mint = payload.get("mint", "").strip()
    if not mint:
        return {"error": "No mint provided"}

    if mint not in active_mints:
        return {"message": f"{mint} not being tracked"}

    active_mints.remove(mint)
    print(f"❌ Removed mint: {mint}")
    print(f"Current tracked mints: {active_mints}")

    # Trigger reconnect in the Solana loop
    asyncio.run_coroutine_threadsafe(trigger_reconnect(), app.state.solana_loop)

    return {"message": f"Stopped tracking {mint}"}

async def trigger_reconnect():
    """Signal the Solana loop to reconnect"""
    reconnect_event.set()

@app.get("/")
def root():
    return {"status": "Solana swap tracker running", "tracked_mints": list(active_mints)}

@app.get("/mints")
def get_mints():
    return {"tracked_mints": list(active_mints)}