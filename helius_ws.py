import asyncio
import json
import logging
import aiofiles
import aiohttp
import websockets
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import signal
import sys
from decimal import Decimal
import time
import os
from dataclasses import dataclass
from enum import Enum

# Configuration
HELIUS_WS_URL = "wss://mainnet.helius-rpc.com/?api-key=ee69b8b0-0db1-4a72-be5d-c507781837d7"
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key=ee69b8b0-0db1-4a72-be5d-c507781837d7"
TRADING_ENDPOINT = 'http://localhost:8081'  # Placeholder trading endpoint
# Trading configuration
STOP_LOSS_PERCENT = 0.25  # 25%
TAKE_PROFIT_PERCENT = 0.50  # 50%

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('token_tracker.log')
    ]
)
logger = logging.getLogger("token_tracker")

class TokenStatus(str, Enum):
    WAITING_FOR_PRICE = "waiting_for_price"
    TRACKING = "under_track"
    EXITED_PROFIT = "exited_with_profit"
    EXITED_STOPLOSS = "exited_with_stoploss"

@dataclass
class TokenState:
    mint: str
    symbol: Optional[str] = None  # Added symbol field
    initial_price: Optional[float] = None
    current_price: Optional[float] = None
    status: TokenStatus = TokenStatus.WAITING_FOR_PRICE
    last_updated: Optional[datetime] = None
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None

class AddTokenRequest(BaseModel):
    mint_address: str = Field(..., description="Token mint address to track")
    symbol: Optional[str] = Field(None, description="Token symbol")
    price_usd: Optional[float] = Field(None, description="Initial price in USD")

class TokenResponse(BaseModel):
    mint: str
    symbol: Optional[str]
    status: TokenStatus
    initial_price: Optional[float]
    current_price: Optional[float]
    profit_loss_percent: Optional[float]
    last_updated: Optional[datetime]

# Global state
class AppState:
    def __init__(self):
        self.tracked_tokens: Dict[str, TokenState] = {}
        self.websocket_connected = False
        self.websocket_task = None
        self.session = None
        self.shutdown_event = asyncio.Event()

app_state = AppState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing application...")
    
    # Initialize aiohttp session
    app_state.session = aiohttp.ClientSession()
    
    # Load existing tokens from file
    await load_tokens_from_file()
    
    # Start WebSocket listener
    app_state.websocket_task = asyncio.create_task(websocket_listener())
    
    yield
    
    # Shutdown
    logger.info("Shutting down application...")
    app_state.shutdown_event.set()
    
    if app_state.websocket_task:
        app_state.websocket_task.cancel()
    
    if app_state.session:
        await app_state.session.close()

app = FastAPI(
    title="Solana Token Tracker API",
    description="Real-time Solana token price tracking with automated trading",
    version="1.0.0",
    lifespan=lifespan
)

# Common quote tokens and DEX programs
QUOTE_TOKENS = {
    "So11111111111111111111111111111111111111112": "SOL"
    # "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    # "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"
}

DEX_PROGRAMS = {
    "Raydium_V4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "Raydium_CLMM": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "Orca_Whirlpool": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "Pump_fun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
}

class PriceTracker:
    def __init__(self):
        self.token_metadata_cache = {}
        self.price_cache = {}
    
    async def get_token_metadata(self, mint_address: str) -> Dict:
        """Get token metadata with caching"""
        if mint_address in self.token_metadata_cache:
            return self.token_metadata_cache[mint_address]
        
        metadata = {'decimals': 6, 'name': mint_address[:8] + '...', 'symbol': mint_address[:4] + '...'}
        
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [mint_address, {"encoding": "jsonParsed"}]
            }
            
            async with app_state.session.post(HELIUS_RPC_URL, json=payload, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'result' in data and data['result']:
                        account_info = data['result']['value']
                        if 'parsed' in account_info and account_info['parsed']:
                            token_info = account_info['parsed']['info']
                            metadata.update({
                                'decimals': token_info.get('decimals', 6),
                                'name': token_info.get('name', metadata['name']),
                                'symbol': token_info.get('symbol', metadata['symbol'])
                            })
        except Exception as e:
            logger.warning(f"Failed to get metadata for {mint_address}: {e}")
        
        self.token_metadata_cache[mint_address] = metadata
        return metadata
    
    async def get_sol_price(self) -> float:
        """Get current SOL price in USD"""
        try:
            url = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
            async with app_state.session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return float(data['data']['So11111111111111111111111111111111111111112']['price'])
        except Exception as e:
            logger.error(f"Error fetching SOL price: {e}")
        
        return 100.0  # Fallback
    
    async def parse_swap_data(self, transaction_data: Dict, mentioned_tokens: List[str]) -> Optional[Dict]:
        """Parse transaction data to extract swap information"""
        if not transaction_data:
            return None
            
        try:
            meta = transaction_data.get('meta', {})
            pre_token_balances = meta.get('preTokenBalances', [])
            post_token_balances = meta.get('postTokenBalances', [])
            
            token_changes = {}
            
            # Process balances
            for balance in pre_token_balances:
                mint = balance.get('mint')
                if mint in mentioned_tokens or mint in QUOTE_TOKENS:
                    ui_token_amount = balance.get('uiTokenAmount', {})
                    token_changes[mint] = {
                        'pre': float(ui_token_amount.get('uiAmount', 0)),
                        'post': 0,
                        'decimals': ui_token_amount.get('decimals', 6)
                    }
            
            for balance in post_token_balances:
                mint = balance.get('mint')
                if mint in mentioned_tokens or mint in QUOTE_TOKENS:
                    ui_token_amount = balance.get('uiTokenAmount', {})
                    if mint in token_changes:
                        token_changes[mint]['post'] = float(ui_token_amount.get('uiAmount', 0))
                    else:
                        token_changes[mint] = {
                            'pre': 0,
                            'post': float(ui_token_amount.get('uiAmount', 0)),
                            'decimals': ui_token_amount.get('decimals', 6)
                        }
            
            # Calculate net changes
            for mint, changes in token_changes.items():
                changes['net'] = changes['post'] - changes['pre']
            
            # Find swap pairs
            for token in mentioned_tokens:
                if token in token_changes:
                    token_change = token_changes[token]['net']
                    
                    for quote_token, quote_symbol in QUOTE_TOKENS.items():
                        if quote_token in token_changes and abs(token_changes[quote_token]['net']) > 0:
                            quote_change = token_changes[quote_token]['net']
                            
                            if token_change != 0 and quote_change != 0:
                                if token_change > 0:  # Token bought
                                    price = abs(quote_change) / abs(token_change)
                                else:  # Token sold
                                    price = abs(quote_change) / abs(token_change)
                                
                                # Convert to USD
                                if quote_token == "So11111111111111111111111111111111111111112":
                                    sol_price = await self.get_sol_price()
                                    price_usd = price * sol_price
                                else:
                                    price_usd = price
                                
                                return {
                                    'token': token,
                                    'price': price,
                                    'price_usd': price_usd,
                                    'token_amount': abs(token_change),
                                    'quote_amount': abs(quote_change),
                                    'quote_token': quote_symbol
                                }
            return None
            
        except Exception as e:
            logger.error(f"Error parsing swap data: {e}")
            return None
    
    async def fetch_transaction(self, signature: str, max_retries: int = 3) -> Optional[Dict]:
        """Fetch transaction with retry logic"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0
                }
            ]
        }

        for attempt in range(1, max_retries + 1):
            try:
                async with app_state.session.post(HELIUS_RPC_URL, json=payload, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        tx = data.get("result")
                        if tx is not None:
                            return tx
                
                # Wait before retry
                delay = 3
                time.sleep(delay * attempt)
            except Exception as e:
                logger.warning(f"[attempt {attempt}] getTransaction failed for {signature}: {e}")
                await asyncio.sleep(attempt * 2)

        logger.error(f"Transaction {signature} not available after {max_retries} tries")
        return None

price_tracker = PriceTracker()

async def load_tokens_from_file():
    """Load tracked tokens from JSON file"""
    try:
        async with aiofiles.open('tracked_tokens.json', 'r') as f:
            content = await f.read()
            if content:
                data = json.loads(content)
                for mint, token_data in data.items():
                    app_state.tracked_tokens[mint] = TokenState(
                        mint=mint,
                        symbol=token_data.get('symbol'),  # Load symbol
                        initial_price=token_data.get('initial_price'),
                        current_price=token_data.get('current_price'),
                        status=TokenStatus(token_data.get('status', 'waiting_for_price')),
                        last_updated=datetime.fromisoformat(token_data['last_updated']) if token_data.get('last_updated') else None,
                        entry_time=datetime.fromisoformat(token_data['entry_time']) if token_data.get('entry_time') else None,
                        exit_time=datetime.fromisoformat(token_data['exit_time']) if token_data.get('exit_time') else None
                    )
        logger.info(f"Loaded {len(app_state.tracked_tokens)} tokens from file")
    except FileNotFoundError:
        logger.info("No existing token file found, starting fresh")
    except Exception as e:
        logger.error(f"Error loading tokens from file: {e}")

async def save_tokens_to_file():
    """Save tracked tokens to JSON file"""
    try:

        data = {}
        for mint, token_state in app_state.tracked_tokens.items():
            data[mint] = {
                'symbol': token_state.symbol,  # Save symbol
                'initial_price': token_state.initial_price,
                'current_price': token_state.current_price,
                'status': token_state.status.value,
                'last_updated': token_state.last_updated.isoformat() if token_state.last_updated else None,
                'entry_time': token_state.entry_time.isoformat() if token_state.entry_time else None,
                'exit_time': token_state.exit_time.isoformat() if token_state.exit_time else None
            }
        
        async with aiofiles.open('tracked_tokens.json', 'w') as f:
            await f.write(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"Error saving tokens to file: {e}")

async def execute_buy_order(token_state: TokenState, price_usd: float):
    """Execute buy order for a token"""
    try:
        # Placeholder for actual buy API call
        url = f"{TRADING_ENDPOINT}/trade/buy"
        payload = {
            "coin_symbol": token_state.symbol,
            "coin_mint": token_state.mint,
            "amount_usdc": price_usd,
            "slippage": 1.0
        }
        bearer_token  = '23265688'
        headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json"
        }
        async with app_state.session.post(url, json=payload, headers= headers, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f" ^^^^^ Buy order response: {data}")
            else:
                logger.error(f"Failed to execute buy order: {response.status} - {await response.text()}")

        logger.info(f"✅ EXECUTING BUY ORDER for {token_state.mint} at ${price_usd:.8f}")
        
        # Update token state
        token_state.initial_price = price_usd
        token_state.current_price = price_usd
        token_state.status = TokenStatus.TRACKING
        token_state.entry_time = datetime.now()
        token_state.last_updated = datetime.now()
        
        await save_tokens_to_file()
        
    except Exception as e:
        logger.error(f"Error executing buy order for {token_state.mint}: {e}")


async def execute_sell_order(token_state: TokenState):
    """Execute sell order for a token"""
    try:
        # Placeholder for actual sell API call
        # This would integrate with your exchange/DEX API
        logger.info(f"🚨 EXECUTING SELL ORDER for {token_state.mint}")
        logger.info(f"   Symbol: {token_state.symbol}")
        logger.info(f"   Initial Price: ${token_state.initial_price:.8f}")
        logger.info(f"   Current Price: ${token_state.current_price:.8f}")
        logger.info(f"   P/L: {((token_state.current_price - token_state.initial_price) / token_state.initial_price * 100):+.2f}%")
        logger.info(f"   Status: {token_state.status}")

        url = f"{TRADING_ENDPOINT}/trade/sell"

        payload ={
            "coin_symbol" : token_state.symbol,
            "coint_mint" : token_state.mint,
            "percentage": 100.0
        }
        bearer_token  = '23265688'
        headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json"
        }

        async with app_state.session.post(url, json=payload, headers= headers, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f" ^^^^^ Sell order response: {data}")
            else:
                logger.error(f"Failed to execute buy order: {response.status} - {await response.text()}")

        # Update token state
        token_state.exit_time = datetime.now()
        await save_tokens_to_file()

        
    except Exception as e:
        logger.error(f"Error executing sell order for {token_state.mint}: {e}")

async def check_trading_conditions(token_state: TokenState):
    """Check if token meets trading conditions"""
    if token_state.status != TokenStatus.TRACKING:
        return
    
    if not token_state.initial_price or not token_state.current_price:
        return
    
    price_change = (token_state.current_price - token_state.initial_price) / token_state.initial_price
    
    if price_change >= TAKE_PROFIT_PERCENT:
        token_state.status = TokenStatus.EXITED_PROFIT
        logger.info(f"🎯 TAKE PROFIT triggered for {token_state.symbol or token_state.mint}: {price_change:.2%}")
        await execute_sell_order(token_state)
    
    elif price_change <= -STOP_LOSS_PERCENT:
        token_state.status = TokenStatus.EXITED_STOPLOSS
        logger.info(f"🛑 STOP LOSS triggered for {token_state.symbol or token_state.mint}: {price_change:.2%}")
        await execute_sell_order(token_state)

async def process_transaction_message(message: str):
    """Process WebSocket message containing transaction data"""
    try:
        parsed = json.loads(message)
        
        # Handle subscription confirmation
        if 'result' in parsed and isinstance(parsed['result'], int):
            return
        
        # Handle transaction logs
        if 'params' in parsed and 'result' in parsed['params']:
            result = parsed['params']['result']
            value = result.get('value', {})
            
            signature = value.get('signature', 'N/A')
            logs = value.get('logs', [])
            
            # Check for tracked tokens in logs
            logs_text = ' '.join(logs)
            mentioned_tokens = [token for token in app_state.tracked_tokens.keys() if token in logs_text]
            
            if mentioned_tokens:
                logger.info(f"🔍 Activity detected for {len(mentioned_tokens)} tracked tokens in {signature}")
                
                # Fetch transaction data
                transaction_data = await price_tracker.fetch_transaction(signature)
                if transaction_data:
                    swap_info = await price_tracker.parse_swap_data(transaction_data, mentioned_tokens)
                    
                    if swap_info and swap_info['token'] in app_state.tracked_tokens:
                        token_state = app_state.tracked_tokens[swap_info['token']]
                        token_metadata = await price_tracker.get_token_metadata(swap_info['token'])
                        
                        # Update token state
                        token_state.current_price = swap_info['price_usd']
                        token_state.last_updated = datetime.now()
                        
                        # If this is the first valid price, set initial price
                        if token_state.status == TokenStatus.WAITING_FOR_PRICE and swap_info['price_usd'] > 0:
                            token_state.initial_price = swap_info['price_usd']
                            token_state.status = TokenStatus.TRACKING
                            token_state.entry_time = datetime.now()
                            logger.info(f"🎯 Started tracking {token_state.symbol or token_metadata.get('name')} at ${swap_info['price_usd']:.8f}")
                        
                        # Check trading conditions
                        await check_trading_conditions(token_state)
                        
                        # Save state
                        print("@@@@@@ Saving tokens to file... for coin "+str(token_state.symbol) or swap_info['token']+" at price ", str(token_state.current_price))
                        await save_tokens_to_file()
                        
                        # Log the update
                        if token_state.status == TokenStatus.TRACKING:
                            price_change = ((token_state.current_price - token_state.initial_price) / 
                                          token_state.initial_price) if token_state.initial_price else 0
                            logger.info(
                                f"📈 {token_state.symbol or token_metadata.get('name')}: "
                                f"${token_state.current_price:.8f} "
                                f"({price_change:+.2%})"
                            )
                    
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")

async def websocket_listener():
    """WebSocket listener with reconnection logic"""
    reconnect_delay = 1
    max_reconnect_delay = 60
    
    while not app_state.shutdown_event.is_set():
        try:
            logger.info(f"🔌 Connecting to WebSocket...")
            
            async with websockets.connect(HELIUS_WS_URL, ping_interval=30, ping_timeout=10) as websocket:
                app_state.websocket_connected = True
                reconnect_delay = 1  # Reset reconnect delay on successful connection
                
                logger.info("✅ WebSocket connected successfully")
                
                # Subscribe to DEX programs
                for dex_name, program_id in DEX_PROGRAMS.items():
                    subscription = {
                        "jsonrpc": "2.0",
                        "id": dex_name,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [program_id]},
                            {"commitment": "confirmed"}
                        ]
                    }
                    await websocket.send(json.dumps(subscription))
                    logger.info(f"✅ Subscribed to {dex_name}")
                
                # Listen for messages
                async for message in websocket:
                    if app_state.shutdown_event.is_set():
                        break
                    
                    # Process message in background to avoid blocking
                    asyncio.create_task(process_transaction_message(message))
                
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        
        app_state.websocket_connected = False
        
        if not app_state.shutdown_event.is_set():
            logger.info(f"🔄 Reconnecting in {reconnect_delay} seconds...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

# API Routes
@app.get("/")
async def root():
    return {
        "message": "Solana Token Tracker API",
        "status": "running",
        "tracked_tokens": len(app_state.tracked_tokens),
        "websocket_connected": app_state.websocket_connected
    }

@app.post("/tokens", response_model=TokenResponse)
async def add_token(request: AddTokenRequest, background_tasks: BackgroundTasks):
    """Add a new token to track"""
    mint = request.mint_address
    coin_symbol = request.symbol
    price_usd = request.price_usd
    
    if mint in app_state.tracked_tokens:
        raise HTTPException(status_code=400, detail="Token already being tracked")
    
    # Validate token exists by fetching metadata
    try:
        metadata = await price_tracker.get_token_metadata(mint)
        logger.info(f"✅ Valid token: {metadata.get('name')} ({metadata.get('symbol')})")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid token mint: {e}")
    
    # Create token state with provided information
    token_state = TokenState(
        mint=mint,
        symbol=coin_symbol if coin_symbol else metadata.get('symbol'),
        initial_price=price_usd,
        current_price=price_usd,
        status=TokenStatus.TRACKING if price_usd else TokenStatus.WAITING_FOR_PRICE,
        entry_time=datetime.now() if price_usd else None,
        last_updated=datetime.now() if price_usd else None
    )

    
    # Add token to tracking
    app_state.tracked_tokens[mint] = token_state
    
    # Save to file
    background_tasks.add_task(save_tokens_to_file)

    # buy immediately if price is provided
    if price_usd:
        print("*******", price_usd)
        background_tasks.add_task(execute_buy_order, token_state, price_usd)
    
    logger.info(f"➕ Added new token to track: {coin_symbol or mint} at ${price_usd if price_usd else 'TBD'}")
    
    return TokenResponse(
        mint=mint,
        symbol=token_state.symbol,
        status=token_state.status,
        initial_price=token_state.initial_price,
        current_price=token_state.current_price,
        profit_loss_percent=None,
        last_updated=token_state.last_updated
    )

@app.get("/tokens", response_model=List[TokenResponse])
async def get_tokens():
    """Get all tracked tokens with their current status"""
    response = []
    for mint, token_state in app_state.tracked_tokens.items():
        profit_loss = None
        if token_state.initial_price and token_state.current_price:
            profit_loss = ((token_state.current_price - token_state.initial_price) / token_state.initial_price) * 100
        
        response.append(TokenResponse(
            mint=mint,
            symbol=token_state.symbol,
            status=token_state.status,
            initial_price=token_state.initial_price,
            current_price=token_state.current_price,
            profit_loss_percent=profit_loss,
            last_updated=token_state.last_updated
        ))
    
    return response

@app.get("/tokens/{mint}", response_model=TokenResponse)
async def get_token(mint: str):
    """Get specific token status"""
    if mint not in app_state.tracked_tokens:
        raise HTTPException(status_code=404, detail="Token not found")
    
    token_state = app_state.tracked_tokens[mint]
    profit_loss = None
    if token_state.initial_price and token_state.current_price:
        profit_loss = ((token_state.current_price - token_state.initial_price) / token_state.initial_price) * 100
    
    return TokenResponse(
        mint=mint,
        symbol=token_state.symbol,
        status=token_state.status,
        initial_price=token_state.initial_price,
        current_price=token_state.current_price,
        profit_loss_percent=profit_loss,
        last_updated=token_state.last_updated
    )

@app.delete("/tokens/{mint}")
async def remove_token(mint: str, background_tasks: BackgroundTasks):
    """Remove token from tracking"""
    if mint not in app_state.tracked_tokens:
        raise HTTPException(status_code=404, detail="Token not found")
    
    del app_state.tracked_tokens[mint]
    background_tasks.add_task(save_tokens_to_file)
    
    logger.info(f"➖ Removed token from tracking: {mint}")
    
    return {"message": f"Token {mint} removed from tracking"}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "websocket_connected": app_state.websocket_connected,
        "tracked_tokens_count": len(app_state.tracked_tokens),
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        access_log=True
    )