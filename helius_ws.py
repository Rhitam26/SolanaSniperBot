import asyncio
import json
import logging
import aiofiles
import aiohttp
import websockets
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import signal
import sys
import time
import os
import random
from dataclasses import dataclass
from enum import Enum
from collections import defaultdict, deque
import socket
import psutil
from pathlib import Path
import gc
import tracemalloc

# ========== CONFIGURATION ==========
# Use environment variables for production
HELIUS_API_KEY = os.getenv('HELIUS_API_KEY', 'ee69b8b0-0db1-4a72-be5d-c507781837d7')
HELIUS_WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
TRADING_ENDPOINT = os.getenv('TRADING_ENDPOINT', 'http://localhost:8081')
TRADING_AUTH_TOKEN = os.getenv('TRADING_AUTH_TOKEN', '23265688')

# Trading configuration
STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '0.55'))
TAKE_PROFIT_PERCENT = float(os.getenv('TAKE_PROFIT_PERCENT', '.70'))

# EC2-specific settings - REDUCED FOR MEMORY OPTIMIZATION
MAX_CONCURRENT_TASKS = int(os.getenv('MAX_CONCURRENT_TASKS', '20'))  # Reduced from 50
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))  # Reduced from 5
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '15'))  # Reduced from 30
MEMORY_LIMIT_MB = int(os.getenv('MEMORY_LIMIT_MB', '512'))

# File paths for EC2
DATA_DIR = os.getenv('DATA_DIR', str(Path.home() / 'token-tracker'))
TOKENS_FILE = os.path.join(DATA_DIR, 'tracked_tokens.json')
LOG_DIR = os.path.join(DATA_DIR, 'logs')

# Memory monitoring
ENABLE_MEMORY_MONITOR = os.getenv('ENABLE_MEMORY_MONITOR', 'true').lower() == 'true'
MEMORY_CHECK_INTERVAL = int(os.getenv('MEMORY_CHECK_INTERVAL', '30'))  # seconds
MEMORY_CLEANUP_THRESHOLD = float(os.getenv('MEMORY_CLEANUP_THRESHOLD', '0.7'))  # 70%

# Ensure directories exist with proper permissions
os.makedirs(DATA_DIR, exist_ok=True, mode=0o755)
os.makedirs(LOG_DIR, exist_ok=True, mode=0o755)

# Create files if they don't exist
if not os.path.exists(TOKENS_FILE):
    with open(TOKENS_FILE, 'w') as f:
        json.dump({}, f)
    os.chmod(TOKENS_FILE, 0o644)

# ========== LOGGING SETUP ==========
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

# Create a formatter
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(process)d] - %(message)s'
)

# Create logger
logger = logging.getLogger("token_tracker")
logger.setLevel(getattr(logging, LOG_LEVEL))

# Clear any existing handlers
logger.handlers.clear()

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# File handler for main log
main_log_path = os.path.join(LOG_DIR, 'token_tracker.log')
file_handler = logging.FileHandler(main_log_path)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# File handler for errors
error_log_path = os.path.join(LOG_DIR, 'error.log')
error_handler = logging.FileHandler(error_log_path)
error_handler.setFormatter(formatter)
logger.addHandler(error_handler)

logger.info(f"Logging initialized. Log directory: {LOG_DIR}")

# ========== DATA MODELS ==========
class TokenStatus(str, Enum):
    WAITING_FOR_PRICE = "waiting_for_price"
    TRACKING = "under_track"
    EXITED_PROFIT = "exited_with_profit"
    EXITED_STOPLOSS = "exited_with_stoploss"

@dataclass
class TokenState:
    mint: str
    symbol: Optional[str] = None
    initial_price: Optional[float] = None
    current_price: Optional[float] = None
    status: TokenStatus = TokenStatus.WAITING_FOR_PRICE
    last_updated: Optional[datetime] = None
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    quantity: Optional[float] = None

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

class HealthStatus(BaseModel):
    status: str
    websocket_connected: bool
    tracked_tokens_count: int
    memory_usage_mb: float
    cpu_percent: float
    active_tasks: int
    uptime_seconds: float
    timestamp: datetime

# ========== UTILITY CLASSES ==========
class SystemMonitor:
    @staticmethod
    def get_memory_usage() -> float:
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024
    
    @staticmethod
    def get_cpu_usage() -> float:
        return psutil.cpu_percent(interval=0.1)
    
    @staticmethod
    def check_memory_limit() -> bool:
        return SystemMonitor.get_memory_usage() > MEMORY_LIMIT_MB
    
    @staticmethod
    def force_garbage_collection():
        """Force garbage collection and clear internal caches"""
        gc.collect()
        if hasattr(gc, 'collect'):
            gc.collect(2)  # Collect older generations

class RateLimiter:
    def __init__(self, max_calls: int = 30, period: int = 1):  # Reduced from 50
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()
    
    async def acquire(self):
        now = time.time()
        while self.calls and self.calls[0] <= now - self.period:
            self.calls.popleft()
        
        if len(self.calls) >= self.max_calls:
            sleep_time = self.calls[0] + self.period - now
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
                now = time.time()
        
        self.calls.append(now)

class TransactionProcessor:
    def __init__(self):
        self.recent_signatures = set()
        self.signature_cache = {}
        self.failed_signatures = defaultdict(int)
        self.rate_limiter = RateLimiter(max_calls=30, period=1)  # Reduced from 40
        self.active_tasks = 0
        self.max_concurrent = MAX_CONCURRENT_TASKS
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.processing_queue = asyncio.Queue(maxsize=100)  # Limit queue size
        self._queue_processor_task = None
        self._cleanup_interval = 60  # seconds
    
    async def start_queue_processor(self):
        """Start processing messages from queue"""
        self._queue_processor_task = asyncio.create_task(self._process_queue())
    
    async def stop_queue_processor(self):
        """Stop the queue processor"""
        if self._queue_processor_task:
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass
    
    async def add_to_queue(self, signature: str, mentioned_tokens: List[str]):
        """Add transaction to processing queue with backpressure"""
        try:
            # Drop old signatures if queue is full
            if self.processing_queue.full():
                # Remove oldest signature from recent_signatures to make room
                if len(self.recent_signatures) > 100:
                    oldest = next(iter(self.recent_signatures))
                    self.recent_signatures.remove(oldest)
            
            await self.processing_queue.put((signature, mentioned_tokens))
        except Exception as e:
            logger.error(f"Error adding to queue: {e}")
    
    async def _process_queue(self):
        """Process items from queue"""
        while True:
            try:
                signature, mentioned_tokens = await self.processing_queue.get()
                await self.process_with_deduplication(signature, mentioned_tokens)
                self.processing_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Queue processor error: {e}")
    
    async def process_with_deduplication(self, signature: str, mentioned_tokens: List[str]):
        """Process transaction with deduplication and memory limits"""
        if signature in self.recent_signatures:
            return
        
        if self.failed_signatures.get(signature, 0) >= 2:  # Reduced from 3
            return
        
        # Check memory before processing
        if SystemMonitor.get_memory_usage() > MEMORY_LIMIT_MB * MEMORY_CLEANUP_THRESHOLD:
            logger.warning("Memory threshold exceeded, skipping transaction")
            return
        
        async with self.semaphore:
            self.active_tasks += 1
            try:
                self.recent_signatures.add(signature)
                
                # Check cache first (with size limit)
                if signature in self.signature_cache:
                    tx = self.signature_cache[signature]
                    await self._process_transaction_data(tx, mentioned_tokens, signature)
                    return
                
                try:
                    await self.rate_limiter.acquire()
                    tx = await price_tracker.fetch_transaction(signature)
                    
                    if tx:
                        # Cache with size limit
                        if len(self.signature_cache) < 200:  # Reduced from 500
                            self.signature_cache[signature] = tx
                        await self._process_transaction_data(tx, mentioned_tokens, signature)
                    else:
                        self.failed_signatures[signature] += 1
                        
                except Exception as e:
                    logger.error(f"Error processing {signature[:16]}...: {e}")
                    self.failed_signatures[signature] += 1
                finally:
                    # Schedule cleanup
                    asyncio.create_task(self._cleanup_signature(signature))
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Unexpected error in processor: {e}")
            finally:
                self.active_tasks -= 1
    
    async def _process_transaction_data(self, tx: Dict, mentioned_tokens: List[str], signature: str):
        """Process transaction data with memory awareness"""
        try:
            swap_info = await price_tracker.parse_swap_data(tx, mentioned_tokens)
            
            if swap_info and swap_info['token'] in app_state.tracked_tokens:
                token_state = app_state.tracked_tokens[swap_info['token']]
                
                if swap_info['price_usd'] and swap_info['price_usd'] > 0:
                    token_state.current_price = swap_info['price_usd']
                    token_state.last_updated = datetime.now()
                    
                    if token_state.status == TokenStatus.WAITING_FOR_PRICE:
                        token_state.initial_price = swap_info['price_usd']
                        token_state.status = TokenStatus.TRACKING
                        token_state.entry_time = datetime.now()
                        logger.info(f"🎯 Started tracking {token_state.symbol or swap_info['token'][:8]}... at ${swap_info['price_usd']:.8f}")
                    
                    await check_trading_conditions(token_state)
                    
                    # Less verbose logging to reduce memory
                    if token_state.status == TokenStatus.TRACKING and token_state.initial_price:
                        price_change = ((token_state.current_price - token_state.initial_price) / 
                                      token_state.initial_price)
                        if abs(price_change) > 0.01:  # Only log significant changes
                            logger.debug(  # Changed from info to debug
                                f"📈 {token_state.symbol or swap_info['token'][:8]}...: "
                                f"${token_state.current_price:.8f} ({price_change:+.2%})"
                            )
                    
                    # Batch file saves to reduce disk I/O
                    app_state.pending_save = True
                    
        except Exception as e:
            logger.error(f"Error processing data for {signature[:16]}...: {e}")
    
    async def _cleanup_signature(self, signature: str):
        """Clean up signature after delay"""
        await asyncio.sleep(self._cleanup_interval)
        if signature in self.recent_signatures:
            self.recent_signatures.remove(signature)
    
    def cleanup_cache(self):
        """Clean up caches with aggressive limits"""
        # Keep only recent signatures
        if len(self.recent_signatures) > 100:
            self.recent_signatures.clear()
        
        # Keep only recent transaction cache
        if len(self.signature_cache) > 100:  # Reduced from 500
            self.signature_cache.clear()
        
        # Clean failed signatures
        if len(self.failed_signatures) > 100:
            self.failed_signatures.clear()

# ========== APPLICATION STATE ==========
class AppState:
    def __init__(self):
        self.tracked_tokens: Dict[str, TokenState] = {}
        self.websocket_connected = False
        self.websocket_task = None
        self.session = None
        self.shutdown_event = asyncio.Event()
        self.transaction_processor = TransactionProcessor()
        self.start_time = datetime.now()
        self.file_lock = asyncio.Lock()
        self.pending_save = False
        self.last_save_time = datetime.now()

app_state = AppState()

# ========== LIFESPAN MANAGER ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Starting application on: {socket.gethostname()}")
    logger.info(f"Memory limit: {MEMORY_LIMIT_MB}MB")
    logger.info(f"Max concurrent tasks: {MAX_CONCURRENT_TASKS}")
    
    # Initialize memory monitoring if enabled
    if ENABLE_MEMORY_MONITOR:
        tracemalloc.start(25)  # Track top 25 memory allocations
    
    # Initialize aiohttp session with optimized settings
    connector = aiohttp.TCPConnector(
        limit=50,  # Reduced from 100
        limit_per_host=20,  # Reduced from 50
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        use_dns_cache=True,
        force_close=True  # Close connections faster
    )
    
    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT,
        connect=5,
        sock_read=10,
        sock_connect=5
    )
    
    app_state.session = aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={'User-Agent': f'SolanaTracker/1.0 (EC2/{socket.gethostname()})'}
    )
    
    # Load existing tokens
    await load_tokens_from_file()
    
    # Start transaction processor queue
    await app_state.transaction_processor.start_queue_processor()
    
    # Start background tasks
    app_state.websocket_task = asyncio.create_task(websocket_listener())
    cleanup_task = asyncio.create_task(periodic_cleanup())
    memory_monitor_task = asyncio.create_task(memory_monitor())
    save_task = asyncio.create_task(periodic_save())  # New: batch saves
    
    yield
    
    # Shutdown
    logger.info("🛑 Shutting down application...")
    app_state.shutdown_event.set()
    
    # Stop transaction processor
    await app_state.transaction_processor.stop_queue_processor()
    
    tasks = [cleanup_task, memory_monitor_task, save_task]
    for task in tasks:
        if task:
            task.cancel()
    
    if app_state.websocket_task:
        app_state.websocket_task.cancel()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    if app_state.session:
        await app_state.session.close()
    
    # Final garbage collection
    SystemMonitor.force_garbage_collection()
    
    if ENABLE_MEMORY_MONITOR:
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics('lineno')[:10]
        logger.info("Top memory allocations:")
        for stat in top_stats:
            logger.info(f"  {stat}")
        tracemalloc.stop()
    
    logger.info("✅ Application shutdown complete")

# ========== FASTAPI APP ==========
app = FastAPI(
    title="Solana Token Tracker API",
    description="Real-time Solana token price tracking with automated trading",
    version="2.1.0",  # Updated version
    docs_url="/docs" if os.getenv('ENVIRONMENT') != 'production' else None,
    redoc_url="/redoc" if os.getenv('ENVIRONMENT') != 'production' else None,
    lifespan=lifespan
)

# ========== HELIUS CONFIG ==========
QUOTE_TOKENS = {
    "So11111111111111111111111111111111111111112": "SOL"
}

DEX_PROGRAMS = {
    "Raydium_V4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "Raydium_CLMM": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "Orca_Whirlpool": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "Pump_fun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
}

# ========== PRICE TRACKER ==========
class PriceTracker:
    def __init__(self):
        self.token_metadata_cache = {}
        self.price_cache = {}
        self.sol_price = 100.0
        self.last_sol_price_update = datetime.now() - timedelta(minutes=5)
        self.cache_hits = 0
        self.cache_misses = 0
    
    async def get_token_metadata(self, mint_address: str) -> Dict:
        # Check cache with size limit
        if mint_address in self.token_metadata_cache:
            self.cache_hits += 1
            return self.token_metadata_cache[mint_address]
        
        self.cache_misses += 1
        metadata = {'decimals': 6, 'name': mint_address[:8] + '...', 'symbol': mint_address[:4] + '...'}
        
        # Only fetch metadata for tokens we're actually tracking
        if mint_address not in app_state.tracked_tokens:
            return metadata
        
        for attempt in range(2):  # Reduced from 3
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getAccountInfo",
                    "params": [mint_address, {"encoding": "jsonParsed"}]
                }
                
                async with app_state.session.post(
                    HELIUS_RPC_URL, 
                    json=payload, 
                    timeout=aiohttp.ClientTimeout(total=3)  # Reduced from 5
                ) as response:
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
                                break
                    elif response.status == 429:
                        await asyncio.sleep(min(2 ** attempt, 5))
                        continue
            except Exception as e:
                if attempt < 1:
                    await asyncio.sleep(1)
        
        # Cache with size limit
        if len(self.token_metadata_cache) < 100:  # Limit cache size
            self.token_metadata_cache[mint_address] = metadata
        else:
            # Remove oldest entries if cache is full
            if self.token_metadata_cache:
                oldest_key = next(iter(self.token_metadata_cache))
                del self.token_metadata_cache[oldest_key]
                self.token_metadata_cache[mint_address] = metadata
        
        return metadata
    
    async def get_sol_price(self) -> float:
        now = datetime.now()
        if (now - self.last_sol_price_update).total_seconds() < 60:  # Increased from 30
            return self.sol_price
        
        try:
            url = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
            async with app_state.session.get(
                url, 
                timeout=aiohttp.ClientTimeout(total=3)  # Reduced from 5
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self.sol_price = float(data['data']['So11111111111111111111111111111111111111112']['price'])
                    self.last_sol_price_update = now
        except Exception as e:
            logger.debug(f"Error fetching SOL price, using cached: {e}")
        
        return self.sol_price
    
    async def parse_swap_data(self, transaction_data: Dict, mentioned_tokens: List[str]) -> Optional[Dict]:
        if not transaction_data:
            return None
            
        try:
            meta = transaction_data.get('meta', {})
            pre_token_balances = meta.get('preTokenBalances', [])
            post_token_balances = meta.get('postTokenBalances', [])
            
            token_changes = {}
            
            # Process only tokens we care about
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
                                price = abs(quote_change) / abs(token_change)
                                
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
            logger.debug(f"Error parsing swap data: {e}")
            return None
    
    async def fetch_transaction(self, signature: str) -> Optional[Dict]:
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

        last_error = None
        
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with app_state.session.post(
                    HELIUS_RPC_URL, 
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)  # Reduced from 10
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        tx = data.get("result")
                        
                        if tx is not None:
                            if self._is_valid_transaction(tx):
                                logger.debug(f"✅ Fetched {signature[:16]}... on attempt {attempt}")
                                return tx
                            else:
                                last_error = "Invalid transaction data"
                        else:
                            last_error = "Transaction not found"
                    
                    elif response.status == 429:
                        wait_time = min(1.5 ** attempt, 10)  # Reduced backoff
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        last_error = f"HTTP {response.status}"
                
                if attempt < MAX_RETRIES:
                    base_wait = min(1.5 * attempt, 8)  # Reduced backoff
                    jitter = random.uniform(0.5, 1.0)
                    wait_time = base_wait * jitter
                    await asyncio.sleep(wait_time)
                    
            except asyncio.TimeoutError:
                last_error = "Timeout"
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(min(2 * attempt, 10))
            except aiohttp.ClientError as e:
                last_error = f"Client error: {str(e)}"
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(min(1.5 * attempt, 8))
            except Exception as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(min(2 * attempt, 10))

        logger.debug(f"❌ Failed to fetch {signature[:16]}...: {last_error}")
        return None

    def _is_valid_transaction(self, tx: Dict) -> bool:
        try:
            return isinstance(tx, dict) and all(key in tx for key in ['transaction', 'meta', 'slot'])
        except Exception:
            return False

price_tracker = PriceTracker()

# ========== FILE OPERATIONS ==========
async def load_tokens_from_file():
    """Load tracked tokens from JSON file with error handling"""
    try:
        async with app_state.file_lock:
            if not os.path.exists(TOKENS_FILE):
                logger.info("No tokens file found, starting fresh")
                return
            
            async with aiofiles.open(TOKENS_FILE, 'r') as f:
                content = await f.read()
                if content.strip():
                    data = json.loads(content)
                    # Load only first 100 tokens if there are too many
                    items = list(data.items())[:100]
                    for mint, token_data in items:
                        app_state.tracked_tokens[mint] = TokenState(
                            mint=mint,
                            symbol=token_data.get('symbol'),
                            initial_price=token_data.get('initial_price'),
                            current_price=token_data.get('current_price'),
                            status=TokenStatus(token_data.get('status', 'waiting_for_price')),
                            last_updated=datetime.fromisoformat(token_data['last_updated']) if token_data.get('last_updated') else None,
                            entry_time=datetime.fromisoformat(token_data['entry_time']) if token_data.get('entry_time') else None,
                            exit_time=datetime.fromisoformat(token_data['exit_time']) if token_data.get('exit_time') else None
                        )
                    logger.info(f"Loaded {len(app_state.tracked_tokens)} tokens from file")
                else:
                    logger.info("Tokens file is empty")
                    
    except Exception as e:
        logger.error(f"Error loading tokens from file: {e}")

async def save_tokens_to_file(force: bool = False):
    """Save tracked tokens to JSON file with batching"""
    if not force and not app_state.pending_save:
        return
    
    now = datetime.now()
    time_since_last_save = (now - app_state.last_save_time).total_seconds()
    
    # Only save if forced or it's been more than 30 seconds since last save
    if not force and time_since_last_save < 30:
        return
    
    temp_file = None
    try:
        async with app_state.file_lock:
            # Only save tokens that have been updated recently
            data = {}
            cutoff_time = datetime.now() - timedelta(hours=24)  # Only save tokens updated in last 24h
            
            for mint, token_state in app_state.tracked_tokens.items():
                if token_state.last_updated and token_state.last_updated > cutoff_time:
                    data[mint] = {
                        'symbol': token_state.symbol,
                        'initial_price': token_state.initial_price,
                        'current_price': token_state.current_price,
                        'status': token_state.status.value,
                        'last_updated': token_state.last_updated.isoformat() if token_state.last_updated else None,
                        'entry_time': token_state.entry_time.isoformat() if token_state.entry_time else None,
                        'exit_time': token_state.exit_time.isoformat() if token_state.exit_time else None,
                        'quantity': token_state.quantity
                    }
            
            # Write to temporary file
            temp_file = f"{TOKENS_FILE}.tmp.{os.getpid()}.{int(time.time())}"
            async with aiofiles.open(temp_file, 'w') as f:
                await f.write(json.dumps(data, indent=2))
            
            # Atomically replace the old file
            os.replace(temp_file, TOKENS_FILE)
            
            app_state.pending_save = False
            app_state.last_save_time = now
            
            logger.debug(f"✅ Saved {len(data)} tokens to file")
            
    except Exception as e:
        logger.error(f"Error saving tokens to file: {e}")
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass

# ========== TRADING FUNCTIONS ==========
async def execute_buy_order(token_state: TokenState, price_usd: float):
    try:
        url = f"{TRADING_ENDPOINT}/trade/buy"
        payload = {
            "coin_symbol": token_state.symbol,
            "coin_mint": token_state.mint,
            "amount_usdc": price_usd,
            "slippage": 1.0
        }
        headers = {
            "Authorization": f"Bearer {TRADING_AUTH_TOKEN}",
            "Content-Type": "application/json"
        }
        
        async with app_state.session.post(
            url, 
            json=payload, 
            headers=headers, 
            timeout=aiohttp.ClientTimeout(total=10)  # Reduced from 15
        ) as response:
            if response.status == 200:
                data = await response.json()
                quantity = data.get('quantity')
                logger.info(f"✅ Buy order response: {data}")
            else:
                logger.error(f"Failed buy order: {response.status} - {await response.text()}")

        logger.info(f"✅ BUY ORDER for {token_state.symbol or token_state.mint[:8]}... at ${price_usd:.8f}")
        token_state.initial_price = price_usd
        token_state.current_price = price_usd
        token_state.quantity = quantity
        token_state.status = TokenStatus.TRACKING
        token_state.entry_time = datetime.now()
        token_state.last_updated = datetime.now()
        
        app_state.pending_save = True
        
    except Exception as e:
        logger.error(f"Error in buy order for {token_state.mint}: {e}")

async def execute_sell_order(token_state: TokenState):
    try:
        url = f"{TRADING_ENDPOINT}/trade/sell"
        payload = {
            "coin_symbol": token_state.symbol,
            "coin_mint": token_state.mint,
            "percentage": 100.0
        }
        headers = {
            "Authorization": f"Bearer {TRADING_AUTH_TOKEN}",
            "Content-Type": "application/json"
        }

        async with app_state.session.post(
            url, 
            json=payload, 
            headers=headers, 
            timeout=aiohttp.ClientTimeout(total=10)  # Reduced from 15
        ) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f"✅ Sell order response: {data}")
            else:
                logger.error(f"Failed sell order: {response.status} - {await response.text()}")

        logger.info(f"🚨 SELL ORDER for {token_state.symbol or token_state.mint[:8]}...")
        
        token_state.exit_time = datetime.now()
        app_state.pending_save = True
        
    except Exception as e:
        logger.error(f"Error in sell order for {token_state.mint}: {e}")

async def check_trading_conditions(token_state: TokenState):
    if token_state.status != TokenStatus.TRACKING:
        return
    
    if not token_state.initial_price or not token_state.current_price:
        return
    
    price_change = (token_state.current_price - token_state.initial_price) / token_state.initial_price
    
    if price_change >= TAKE_PROFIT_PERCENT:
        token_state.status = TokenStatus.EXITED_PROFIT
        logger.info(f"🎯 TAKE PROFIT for {token_state.symbol or token_state.mint[:8]}...: {price_change:.2%}")
        await execute_sell_order(token_state)
    
    elif price_change <= -STOP_LOSS_PERCENT:
        token_state.status = TokenStatus.EXITED_STOPLOSS
        logger.info(f"🛑 STOP LOSS for {token_state.symbol or token_state.mint[:8]}...: {price_change:.2%}")
        await execute_sell_order(token_state)

# ========== WEBSOCKET HANDLING ==========
async def process_transaction_message(message: str):
    """Process transaction message with rate limiting"""
    try:
        parsed = json.loads(message)
        
        if 'result' in parsed and isinstance(parsed['result'], int):
            return
        
        if 'params' in parsed and 'result' in parsed['params']:
            result = parsed['params']['result']
            value = result.get('value', {})
            
            signature = value.get('signature', '')
            if not signature or signature == 'N/A':
                return
            
            logs = value.get('logs', [])
            logs_text = ' '.join(logs)
            
            # Check memory before processing
            if SystemMonitor.get_memory_usage() > MEMORY_LIMIT_MB * 0.8:
                logger.warning("Memory high, skipping message processing")
                return
            
            mentioned_tokens = [token for token in app_state.tracked_tokens.keys() if token in logs_text]
            
            if mentioned_tokens:
                # Use queue instead of creating tasks directly
                await app_state.transaction_processor.add_to_queue(signature, mentioned_tokens)
                
    except json.JSONDecodeError as e:
        logger.error(f"JSON error: {e}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")

async def websocket_listener():
    reconnect_delay = 1
    max_reconnect_delay = 60  # Reduced from 300
    
    while not app_state.shutdown_event.is_set():
        try:
            logger.info(f"🔌 Connecting to WebSocket...")
            
            async with websockets.connect(
                HELIUS_WS_URL, 
                ping_interval=20,  # Reduced from 30
                ping_timeout=5,    # Reduced from 10
                close_timeout=3,   # Reduced from 5
                max_size=2**20     # Reduced from 2**23
            ) as websocket:
                app_state.websocket_connected = True
                reconnect_delay = 1
                
                logger.info("✅ WebSocket connected")
                
                # Subscribe only to Pump_fun to reduce load
                subscription = {
                    "jsonrpc": "2.0",
                    "id": "Pump_fun",
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [DEX_PROGRAMS["Pump_fun"]]},
                        {"commitment": "confirmed"}
                    ]
                }
                await websocket.send(json.dumps(subscription))
                logger.info(f"✅ Subscribed to Pump_fun")
                
                async for message in websocket:
                    if app_state.shutdown_event.is_set():
                        break
                    
                    # Process message directly instead of creating task
                    await process_transaction_message(message)
                
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e.code} - {e.reason}")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        
        app_state.websocket_connected = False
        
        if not app_state.shutdown_event.is_set():
            logger.info(f"🔄 Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)  # Slower backoff

# ========== BACKGROUND TASKS ==========
async def periodic_cleanup():
    """Periodic cleanup of caches and memory"""
    while not app_state.shutdown_event.is_set():
        try:
            await asyncio.sleep(180)  # Increased from 300
            app_state.transaction_processor.cleanup_cache()
            
            # Aggressive cache clearing
            if len(price_tracker.token_metadata_cache) > 50:
                price_tracker.token_metadata_cache.clear()
            
            # Clear price cache
            if len(price_tracker.price_cache) > 50:
                price_tracker.price_cache.clear()
            
            # Force garbage collection periodically
            SystemMonitor.force_garbage_collection()
            
            logger.debug(f"🔄 Cleaned up caches. Memory: {SystemMonitor.get_memory_usage():.1f}MB")
            
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

async def memory_monitor():
    """Monitor memory usage and take action when high"""
    while not app_state.shutdown_event.is_set():
        try:
            await asyncio.sleep(MEMORY_CHECK_INTERVAL)
            
            memory_usage = SystemMonitor.get_memory_usage()
            
            if memory_usage > MEMORY_LIMIT_MB * 0.9:
                logger.error(f"🚨 CRITICAL memory usage: {memory_usage:.1f}MB")
                # Emergency cleanup
                app_state.transaction_processor.cleanup_cache()
                price_tracker.token_metadata_cache.clear()
                price_tracker.price_cache.clear()
                SystemMonitor.force_garbage_collection()
                
                # Drop some tracked tokens if still high
                if memory_usage > MEMORY_LIMIT_MB * 0.95:
                    logger.warning("Dropping old tokens to free memory")
                    tokens_to_remove = []
                    cutoff = datetime.now() - timedelta(hours=6)
                    for mint, token in app_state.tracked_tokens.items():
                        if token.last_updated and token.last_updated < cutoff:
                            tokens_to_remove.append(mint)
                    
                    for mint in tokens_to_remove[:10]:  # Remove up to 10 old tokens
                        del app_state.tracked_tokens[mint]
                    
                    app_state.pending_save = True
                
            elif memory_usage > MEMORY_LIMIT_MB * 0.7:
                logger.warning(f"⚠️ High memory usage: {memory_usage:.1f}MB")
                app_state.transaction_processor.cleanup_cache()
                
            if SystemMonitor.check_memory_limit():
                logger.error(f"🚨 Memory limit exceeded: {memory_usage:.1f}MB")
                
        except Exception as e:
            logger.error(f"Memory monitor error: {e}")

async def periodic_save():
    """Periodically save tokens to file"""
    while not app_state.shutdown_event.is_set():
        try:
            await asyncio.sleep(30)  # Save every 30 seconds
            await save_tokens_to_file()
        except Exception as e:
            logger.error(f"Periodic save error: {e}")

# ========== API ROUTES ==========
@app.get("/", include_in_schema=False)
async def root():
    uptime = datetime.now() - app_state.start_time
    memory_usage = SystemMonitor.get_memory_usage()
    return {
        "message": "Solana Token Tracker API",
        "version": "2.1.0",
        "environment": os.getenv('ENVIRONMENT', 'development'),
        "instance": socket.gethostname(),
        "status": "running",
        "uptime": str(uptime),
        "memory_usage_mb": round(memory_usage, 2),
        "tracked_tokens": len(app_state.tracked_tokens),
        "active_tasks": app_state.transaction_processor.active_tasks
    }

@app.post("/tokens", response_model=TokenResponse)
async def add_token(request: AddTokenRequest, background_tasks: BackgroundTasks):
    mint = request.mint_address
    
    # Limit number of tracked tokens
    if len(app_state.tracked_tokens) >= 100:
        raise HTTPException(status_code=400, detail="Maximum token limit (100) reached")
    
    if mint in app_state.tracked_tokens:
        raise HTTPException(status_code=400, detail="Token already tracked")
    
    try:
        metadata = await price_tracker.get_token_metadata(mint)
        logger.info(f"✅ Valid token: {metadata.get('name')} ({metadata.get('symbol')})")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid token: {e}")
    
    token_state = TokenState(
        mint=mint,
        symbol=request.symbol if request.symbol else metadata.get('symbol'),
        initial_price=request.price_usd,
        current_price=request.price_usd,
        status=TokenStatus.TRACKING if request.price_usd else TokenStatus.WAITING_FOR_PRICE,
        entry_time=datetime.now() if request.price_usd else None,
        last_updated=datetime.now() if request.price_usd else None
    )
    
    app_state.tracked_tokens[mint] = token_state
    app_state.pending_save = True
    
    if request.price_usd:
        background_tasks.add_task(execute_buy_order, token_state, request.price_usd)
    
    logger.info(f"➕ Added token: {request.symbol or mint} at ${request.price_usd if request.price_usd else 'TBD'}")
    
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
async def remove_token(mint: str):
    if mint not in app_state.tracked_tokens:
        raise HTTPException(status_code=404, detail="Token not found")
    
    del app_state.tracked_tokens[mint]
    app_state.pending_save = True
    
    logger.info(f"➖ Removed token: {mint}")
    
    return {"message": f"Token {mint} removed"}

@app.get("/health", response_model=HealthStatus)
async def health_check():
    memory_usage = SystemMonitor.get_memory_usage()
    cpu_usage = SystemMonitor.get_cpu_usage()
    uptime = (datetime.now() - app_state.start_time).total_seconds()
    
    return HealthStatus(
        status="healthy",
        websocket_connected=app_state.websocket_connected,
        tracked_tokens_count=len(app_state.tracked_tokens),
        memory_usage_mb=round(memory_usage, 2),
        cpu_percent=round(cpu_usage, 2),
        active_tasks=app_state.transaction_processor.active_tasks,
        uptime_seconds=round(uptime, 2),
        timestamp=datetime.now()
    )

@app.get("/metrics")
async def metrics():
    memory_usage = SystemMonitor.get_memory_usage()
    cpu_usage = SystemMonitor.get_cpu_usage()
    
    metrics_data = f"""# HELP token_tracker_memory_usage Memory usage in MB
# TYPE token_tracker_memory_usage gauge
token_tracker_memory_usage {memory_usage}

# HELP token_tracker_cpu_usage CPU usage percentage
# TYPE token_tracker_cpu_usage gauge
token_tracker_cpu_usage {cpu_usage}

# HELP token_tracker_tokens_count Number of tracked tokens
# TYPE token_tracker_tokens_count gauge
token_tracker_tokens_count {len(app_state.tracked_tokens)}

# HELP token_tracker_websocket_connected WebSocket connection status
# TYPE token_tracker_websocket_connected gauge
token_tracker_websocket_connected {1 if app_state.websocket_connected else 0}

# HELP token_tracker_active_tasks Number of active tasks
# TYPE token_tracker_active_tasks gauge
token_tracker_active_tasks {app_state.transaction_processor.active_tasks}
"""
    
    return Response(content=metrics_data, media_type="text/plain")

# ========== MAIN ENTRY POINT ==========
if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '8080'))
    
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        app_state.shutdown_event.set()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Check write permissions
    test_file = os.path.join(DATA_DIR, '.test_write')
    try:
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        logger.info(f"✓ Write permission verified for {DATA_DIR}")
    except Exception as e:
        logger.error(f"✗ Cannot write to {DATA_DIR}: {e}")
        sys.exit(1)
    
    # Configure uvicorn for memory efficiency
    uvicorn_config = {
        "host": host,
        "port": port,
        "log_level": "info",
        "access_log": False,
        "timeout_keep_alive": 20,  # Reduced from 30
        "limit_concurrency": 100,   # Reduced from 1000
        "limit_max_requests": 1000, # Reduced from 10000
        "workers": 1  # Single worker for memory efficiency
    }
    
    # Add loop optimizations for memory
    if hasattr(asyncio, 'WindowsProactorEventLoopPolicy') and isinstance(
        asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy
    ):
        # Windows specific
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Set lower memory limits for asyncio
    asyncio.get_event_loop().slow_callback_duration = 1.0  # Warn on slow callbacks
    
    uvicorn.run(app, **uvicorn_config)
