import asyncio
import websockets
import json
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn

# Pydantic models for API requests
class PoolConfig(BaseModel):
    pool_name: str
    pool_address: str
    token_a_mint: str
    token_b_mint: str
    token_a_symbol: str = "TokenA"
    token_b_symbol: str = "TokenB"

class SwapEvent(BaseModel):
    timestamp: str
    signature: str
    pool_name: str
    wallet: str
    token_in_symbol: str
    token_out_symbol: str
    amount_in: float
    amount_out: float
    price: float
    solscan_url: str

# FastAPI app
app = FastAPI(title="Multi-Pool Swap Tracker API", version="1.0.0")

class MultiPoolSwapTracker:
    def __init__(self, helius_api_key):
        self.helius_api_key = helius_api_key
        self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self.pools = {}
        self.subscription_id = None
        self.websocket_connection = None
        self.connected_clients = []
        self.is_running = False
        self.reconnect_task = None
        
    def add_pool(self, pool_name, pool_address, token_a_mint, token_b_mint, 
                 token_a_symbol="TokenA", token_b_symbol="TokenB"):
        """Add a pool to track"""
        self.pools[pool_address] = {
            "name": pool_name,
            "address": pool_address,
            "token_a_mint": token_a_mint,
            "token_b_mint": token_b_mint,
            "token_a_symbol": token_a_symbol,
            "token_b_symbol": token_b_symbol,
            "token_map": {
                token_a_mint: token_a_symbol,
                token_b_mint: token_b_symbol
            }
        }
        print(f"✓ Added pool: {pool_name} ({pool_address[:8]}...)")
        return True
        
    async def resubscribe(self):
        """Resubscribe to all pools after adding a new one"""
        # Close existing connection and reconnect with new pool list
        if self.websocket_connection and not self.websocket_connection.closed:
            try:
                await self.websocket_connection.close()
                print("✓ Closed connection to resubscribe with new pool list")
                # The main loop will automatically reconnect
                return True
            except Exception as e:
                print(f"Error closing connection for resubscribe: {e}")
                return False
        return False
        
    async def connect_and_subscribe(self):
        """Connect to Helius WebSocket and subscribe to all pools"""
        if not self.pools:
            print("Warning: No pools configured initially.")
            
        while self.is_running:
            try:
                async with websockets.connect(self.ws_url) as websocket:
                    self.websocket_connection = websocket
                    print(f"\n{'='*80}")
                    print(f"Connected to Helius WebSocket")
                    print(f"Monitoring {len(self.pools)} pool(s)")
                    print(f"{'='*80}\n")
                    
                    if self.pools:
                        pool_addresses = list(self.pools.keys())
                        
                        subscribe_request = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "transactionSubscribe",
                            "params": [
                                {
                                    "mentions": pool_addresses,
                                    "failed": False
                                },
                                {
                                    "commitment": "confirmed",
                                    "encoding": "jsonParsed",
                                    "transactionDetails": "full",
                                    "showRewards": False,
                                    "maxSupportedTransactionVersion": 0
                                }
                            ]
                        }
                        
                        await websocket.send(json.dumps(subscribe_request))
                        response = await websocket.recv()
                        response_data = json.loads(response)
                        
                        if "result" in response_data:
                            self.subscription_id = response_data["result"]
                            print(f"✓ Subscription confirmed (ID: {self.subscription_id})")
                    
                    async for message in websocket:
                        await self.handle_message(message)
                        
            except websockets.exceptions.ConnectionClosed:
                print("WebSocket connection closed. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(5)
                
    async def handle_message(self, message):
        """Process incoming WebSocket messages"""
        try:
            data = json.loads(message)
            
            if "method" in data and data["method"] == "transactionNotification":
                transaction = data.get("params", {}).get("result", {})
                await self.parse_swap_transaction(transaction)
                
        except Exception as e:
            print(f"Error parsing message: {e}")
            
    def identify_pool(self, transaction):
        """Identify which pool this transaction belongs to"""
        try:
            tx_data = transaction.get("transaction", {})
            account_keys = tx_data.get("message", {}).get("accountKeys", [])
            
            for account in account_keys:
                pubkey = account.get("pubkey") if isinstance(account, dict) else account
                if pubkey in self.pools:
                    return self.pools[pubkey]
                    
            return None
        except Exception as e:
            return None
            
    async def parse_swap_transaction(self, transaction):
        """Parse and display swap transaction details"""
        try:
            pool_config = self.identify_pool(transaction)
            if not pool_config:
                return
            
            tx_data = transaction.get("transaction", {})
            meta = tx_data.get("meta", {})
            signature = transaction.get("signature", "N/A")
            
            if meta.get("err") is not None:
                return
            
            pre_balances = meta.get("preTokenBalances", [])
            post_balances = meta.get("postTokenBalances", [])
            
            swap_info = self.extract_swap_info(
                pre_balances, 
                post_balances, 
                tx_data, 
                pool_config
            )
            
            if swap_info:
                await self.broadcast_swap(signature, swap_info, pool_config)
                
        except Exception as e:
            print(f"Error parsing transaction: {e}")
            
    def extract_swap_info(self, pre_balances, post_balances, tx_data, pool_config):
        """Extract swap amounts and direction from token balances"""
        try:
            token_changes = {}
            relevant_mints = [pool_config["token_a_mint"], pool_config["token_b_mint"]]
            
            for pre in pre_balances:
                mint = pre.get("mint")
                
                if mint not in relevant_mints:
                    continue
                    
                account = pre.get("accountIndex")
                pre_amount = float(pre.get("uiTokenAmount", {}).get("uiAmount", 0))
                
                post = next((p for p in post_balances 
                           if p.get("accountIndex") == account), None)
                
                if post:
                    post_amount = float(post.get("uiTokenAmount", {}).get("uiAmount", 0))
                    change = post_amount - pre_amount
                    
                    if abs(change) > 0.000001:
                        token_changes[mint] = change
            
            if len(token_changes) >= 2:
                token_in = None
                token_out = None
                amount_in = 0
                amount_out = 0
                
                for mint, change in token_changes.items():
                    if change < 0:
                        token_in = mint
                        amount_in = abs(change)
                    elif change > 0:
                        token_out = mint
                        amount_out = change
                
                if token_in and token_out and amount_in > 0 and amount_out > 0:
                    wallet = tx_data.get("message", {}).get("accountKeys", [{}])[0].get("pubkey", "Unknown")
                    
                    return {
                        "token_in": token_in,
                        "token_out": token_out,
                        "amount_in": amount_in,
                        "amount_out": amount_out,
                        "wallet": wallet,
                        "price": amount_out / amount_in if amount_in > 0 else 0
                    }
            
            return None
            
        except Exception as e:
            print(f"Error extracting swap info: {e}")
            return None
            
    async def broadcast_swap(self, signature, swap_info, pool_config):
        """Broadcast swap to all connected WebSocket clients"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        token_in_symbol = pool_config["token_map"].get(
            swap_info["token_in"], 
            swap_info["token_in"][:8]
        )
        token_out_symbol = pool_config["token_map"].get(
            swap_info["token_out"], 
            swap_info["token_out"][:8]
        )
        
        swap_event = SwapEvent(
            timestamp=timestamp,
            signature=signature,
            pool_name=pool_config["name"],
            wallet=swap_info["wallet"],
            token_in_symbol=token_in_symbol,
            token_out_symbol=token_out_symbol,
            amount_in=swap_info["amount_in"],
            amount_out=swap_info["amount_out"],
            price=swap_info["price"],
            solscan_url=f"https://solscan.io/tx/{signature}"
        )
        
        # Print to console
        print(f"🔄 SWAP in {pool_config['name']} at {timestamp}")
        print(f"   Signature: {signature[:16]}...{signature[-16:]}")
        print(f"   Wallet: {swap_info['wallet'][:8]}...{swap_info['wallet'][-8:]}")
        print(f"   Swap: {swap_info['amount_in']:.6f} {token_in_symbol} → {swap_info['amount_out']:.6f} {token_out_symbol}")
        print(f"   Price: {swap_info['price']:.8f} {token_out_symbol}/{token_in_symbol}")
        print("-" * 80 + "\n")
        
        # Broadcast to all connected WebSocket clients
        disconnected_clients = []
        for client in self.connected_clients:
            try:
                await client.send_json(swap_event.dict())
            except Exception as e:
                print(f"Error sending to client: {e}")
                disconnected_clients.append(client)
        
        # Remove disconnected clients
        for client in disconnected_clients:
            self.connected_clients.remove(client)

    async def start(self):
        """Start the tracker"""
        self.is_running = True
        await self.connect_and_subscribe()
    
    def stop(self):
        """Stop the tracker"""
        self.is_running = False


# Global tracker instance
HELIUS_API_KEY = "ee69b8b0-0db1-4a72-be5d-c507781837d7"
tracker = MultiPoolSwapTracker(helius_api_key=HELIUS_API_KEY)

# Initialize with default pools
tracker.add_pool(
    pool_name="SOL-USDC",
    pool_address="58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
    token_a_mint="So11111111111111111111111111111111111111112",
    token_b_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    token_a_symbol="SOL",
    token_b_symbol="USDC"
)

tracker.add_pool(
    pool_name="SOL-CUMSHOT",
    pool_address="DsHTtJrXKJzsChWLiCN8y5iDEXtN1PNaMBbrszSFAHYS",
    token_a_mint="So11111111111111111111111111111111111111112",
    token_b_mint="5s3cEXBY51wVkUtiGgrrha3Su9EpzyNiEyh8n1hMJRTE",
    token_a_symbol="SOL",
    token_b_symbol="CUMSHOT"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown"""
    # Startup
    print("✓ Starting swap tracker...")
    tracker_task = asyncio.create_task(tracker.start())
    print("✓ Swap tracker started")
    
    yield
    
    # Shutdown
    print("✓ Stopping swap tracker...")
    tracker.stop()
    
    # Wait for the tracker task to complete or timeout
    try:
        await asyncio.wait_for(tracker_task, timeout=5.0)
    except asyncio.TimeoutError:
        print("✓ Tracker task shutdown timeout, cancelling...")
        tracker_task.cancel()
        try:
            await tracker_task
        except asyncio.CancelledError:
            pass
    
    # Close WebSocket connection if still open
    if tracker.websocket_connection and not tracker.websocket_connection.closed:
        await tracker.websocket_connection.close()
    
    print("✓ Swap tracker stopped")


# FastAPI app with lifespan
app = FastAPI(
    title="Multi-Pool Swap Tracker API",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Multi-Pool Swap Tracker API",
        "endpoints": {
            "GET /pools": "List all tracked pools",
            "POST /pools": "Add a new pool to track",
            "DELETE /pools/{pool_address}": "Remove a pool",
            "WebSocket /ws": "Connect to receive real-time swap events"
        }
    }


@app.get("/pools")
async def list_pools():
    """List all currently tracked pools"""
    pools_list = []
    for address, config in tracker.pools.items():
        pools_list.append({
            "pool_name": config["name"],
            "pool_address": address,
            "token_a_mint": config["token_a_mint"],
            "token_b_mint": config["token_b_mint"],
            "token_a_symbol": config["token_a_symbol"],
            "token_b_symbol": config["token_b_symbol"]
        })
    
    return {
        "total_pools": len(pools_list),
        "pools": pools_list
    }


@app.post("/pools")
async def add_pool(pool: PoolConfig):
    """Add a new pool to track"""
    try:
        # Check if pool already exists
        if pool.pool_address in tracker.pools:
            raise HTTPException(status_code=400, detail="Pool already exists")
        
        # Add the pool
        tracker.add_pool(
            pool_name=pool.pool_name,
            pool_address=pool.pool_address,
            token_a_mint=pool.token_a_mint,
            token_b_mint=pool.token_b_mint,
            token_a_symbol=pool.token_a_symbol,
            token_b_symbol=pool.token_b_symbol
        )
        
        # Resubscribe to include the new pool
        await tracker.resubscribe()
        
        return {
            "status": "success",
            "message": f"Pool {pool.pool_name} added successfully",
            "pool": pool.dict()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/pools/{pool_address}")
async def remove_pool(pool_address: str):
    """Remove a pool from tracking"""
    if pool_address not in tracker.pools:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    pool_name = tracker.pools[pool_address]["name"]
    del tracker.pools[pool_address]
    
    # Resubscribe without the removed pool
    await tracker.resubscribe()
    
    return {
        "status": "success",
        "message": f"Pool {pool_name} removed successfully"
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time swap events"""
    await websocket.accept()
    tracker.connected_clients.append(websocket)
    
    try:
        await websocket.send_json({
            "type": "connection",
            "message": "Connected to swap tracker",
            "tracked_pools": len(tracker.pools)
        })
        
        # Keep connection alive
        while True:
            # Wait for any message from client (ping/pong)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                await websocket.send_json({"type": "ping"})
                
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        if websocket in tracker.connected_clients:
            tracker.connected_clients.remove(websocket)


if __name__ == "__main__":
    uvicorn.run(
        "helius_ws:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info"
    )