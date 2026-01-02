import asyncio
import json
import logging
import aiofiles
import aiohttp
import websockets
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import Dict, List, Optional, Set, Any
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
from cachetools import LRUCache, TTLCache
import hashlib
from prometheus_client import Counter, Gauge, Histogram, generate_latest, REGISTRY
from prometheus_client.exposition import MetricsHandler
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from circuitbreaker import circuit
import backoff
import secrets
from typing_extensions import Literal
import httpx
from concurrent.futures import ThreadPoolExecutor
import uvicorn

# ========== CONFIGURATION ==========
class Config:
    # Security: NO HARDCODED DEFAULTS FOR SECRETS
    HELIUS_API_KEY = os.environ['HELIUS_API_KEY']  # Will raise error if not set
    TRADING_AUTH_TOKEN = os.environ['TRADING_AUTH_TOKEN']  # Will raise error if not set
    SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(32))
    
    # API Endpoints
    HELIUS_WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    TRADING_ENDPOINT = os.getenv('TRADING_ENDPOINT', 'http://localhost:8081')
    
    # Trading configuration
    STOP_LOSS_PERCENT = float(os.getenv('STOP_LOSS_PERCENT', '0.55'))
    TAKE_PROFIT_PERCENT = float(os.getenv('TAKE_PROFIT_PERCENT', '0.70'))
    
    # Performance tuning
    MAX_CONCURRENT_TASKS = int(os.getenv('MAX_CONCURRENT_TASKS', '30'))
    MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
    REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '15'))
    MEMORY_LIMIT_MB = int(os.getenv('MEMORY_LIMIT_MB', '1024'))
    MAX_TRACKED_TOKENS = int(os.getenv('MAX_TRACKED_TOKENS', '200'))
    
    # File paths
    DATA_DIR = Path(os.getenv('DATA_DIR', '/var/lib/token-tracker'))
    TOKENS_FILE = DATA_DIR / 'tracked_tokens.json'
    BACKUP_DIR = DATA_DIR / 'backups'
    LOG_DIR = DATA_DIR / 'logs'
    
    # Monitoring
    ENABLE_MEMORY_MONITOR = os.getenv('ENABLE_MEMORY_MONITOR', 'true').lower() == 'true'
    MEMORY_CHECK_INTERVAL = int(os.getenv('MEMORY_CHECK_INTERVAL', '30'))
    MEMORY_CLEANUP_THRESHOLD = float(os.getenv('MEMORY_CLEANUP_THRESHOLD', '0.7'))
    
    # Health check thresholds
    HEALTH_MEMORY_THRESHOLD = float(os.getenv('HEALTH_MEMORY_THRESHOLD', '0.9'))
    HEALTH_CPU_THRESHOLD = float(os.getenv('HEALTH_CPU_THRESHOLD', '90.0'))
    
    # WebSocket
    WS_RECONNECT_BASE = float(os.getenv('WS_RECONNECT_BASE', '1.0'))
    WS_RECONNECT_MAX = float(os.getenv('WS_RECONNECT_MAX', '60.0'))
    
    @classmethod
    def setup_directories(cls):
        """Create necessary directories with proper permissions"""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
        cls.BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
        cls.LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
        
        # Set ownership if running as root
        if os.getuid() == 0:
            os.chown(cls.DATA_DIR, 1000, 1000)  # Assume uid 1000 for app user
            os.chown(cls.BACKUP_DIR, 1000, 1000)
            os.chown(cls.LOG_DIR, 1000, 1000)

Config.setup_directories()

# ========== METRICS ==========
class Metrics:
    # Counters
    transactions_processed = Counter('transactions_processed', 'Total transactions processed')
    transactions_failed = Counter('transactions_failed', 'Failed transaction processing')
    tokens_added = Counter('tokens_added', 'Tokens added to tracking')
    tokens_removed = Counter('tokens_removed', 'Tokens removed from tracking')
    trades_executed = Counter('trades_executed', 'Trades executed', ['type', 'status'])
    websocket_reconnects = Counter('websocket_reconnects', 'WebSocket reconnection attempts')
    api_requests = Counter('api_requests', 'API requests', ['endpoint', 'method', 'status'])
    
    # Gauges
    tracked_tokens = Gauge('tracked_tokens', 'Number of tracked tokens')
    active_tasks = Gauge('active_tasks', 'Number of active processing tasks')
    memory_usage = Gauge('memory_usage_mb', 'Memory usage in MB')
    cpu_usage = Gauge('cpu_usage_percent', 'CPU usage percentage')
    queue_size = Gauge('processing_queue_size', 'Size of processing queue')
    cache_size = Gauge('cache_size', 'Size of various caches', ['cache_type'])
    
    # Histograms
    transaction_latency = Histogram('transaction_latency_seconds', 'Transaction processing latency')
    api_latency = Histogram('api_latency_seconds', 'API endpoint latency', ['endpoint'])
    websocket_message_size = Histogram('websocket_message_size_bytes', 'WebSocket message size')

# ========== LOGGING SETUP ==========
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'process_id': record.process,
            'thread_id': record.thread,
            'message': record.getMessage(),
            'hostname': socket.gethostname(),
        }
        
        if record.exc_info:
            log_record['exception'] = self.formatException(record.exc_info)
        
        if hasattr(record, 'extra'):
            log_record.update(record.extra)
        
        return json.dumps(log_record)

def setup_logging():
    """Configure structured logging"""
    logger = logging.getLogger("token_tracker")
    logger.setLevel(getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper()))
    logger.propagate = False
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Console handler with JSON
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JSONFormatter())
    logger.addHandler(console_handler)
    
    # File handler for all logs
    file_handler = logging.FileHandler(Config.LOG_DIR / 'app.log')
    file_handler.setFormatter(JSONFormatter())
    logger.addHandler(file_handler)
    
    # Error file handler
    error_handler = logging.FileHandler(Config.LOG_DIR / 'error.log')
    error_handler.setFormatter(JSONFormatter())
    error_handler.setLevel(logging.ERROR)
    logger.addHandler(error_handler)
    
    # Set log level for third-party libraries
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)
    
    return logger

logger = setup_logging()

# ========== DATA MODELS ==========
class TokenStatus(str, Enum):
    WAITING_FOR_PRICE = "waiting_for_price"
    TRACKING = "tracking"
    EXITED_PROFIT = "exited_with_profit"
    EXITED_STOPLOSS = "exited_with_stoploss"
    EXPIRED = "expired"  # Token no longer active

@dataclass
class TokenMetadata:
    mint: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    decimals: int = 6
    last_verified: Optional[datetime] = None

@dataclass
class TokenState:
    mint: str
    metadata: TokenMetadata
    initial_price: Optional[float] = None
    current_price: Optional[float] = None
    status: TokenStatus = TokenStatus.WAITING_FOR_PRICE
    last_updated: Optional[datetime] = None
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    quantity: Optional[float] = None
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    
    @property
    def symbol(self) -> Optional[str]:
        return self.metadata.symbol
    
    @property
    def profit_loss_percent(self) -> Optional[float]:
        if self.initial_price and self.current_price:
            return ((self.current_price - self.initial_price) / self.initial_price) * 100
        return None
    
    def update_price(self, price: float):
        """Update price with tracking of highs/lows"""
        self.current_price = price
        self.last_updated = datetime.now()
        
        if self.highest_price is None or price > self.highest_price:
            self.highest_price = price
            
        if self.lowest_price is None or price < self.lowest_price:
            self.lowest_price = price

class AddTokenRequest(BaseModel):
    mint_address: str = Field(..., min_length=32, max_length=44, description="Token mint address")
    symbol: Optional[str] = Field(None, min_length=1, max_length=10)
    initial_price_usd: Optional[float] = Field(None, gt=0, description="Initial price in USD")
    
    @validator('mint_address')
    def validate_mint_address(cls, v):
        # Basic Solana address validation
        if not all(c in '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz' for c in v):
            raise ValueError('Invalid mint address format')
        return v

class TokenResponse(BaseModel):
    mint: str
    symbol: Optional[str]
    name: Optional[str]
    status: TokenStatus
    initial_price: Optional[float]
    current_price: Optional[float]
    profit_loss_percent: Optional[float]
    highest_price: Optional[float]
    lowest_price: Optional[float]
    last_updated: Optional[datetime]
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]

class HealthStatus(BaseModel):
    status: Literal['healthy', 'degraded', 'unhealthy']
    checks: Dict[str, bool]
    websocket_connected: bool
    tracked_tokens_count: int
    memory_usage_mb: float
    cpu_percent: float
    active_tasks: int
    queue_size: int
    uptime_seconds: float
    version: str = "3.0.0"
    timestamp: datetime

# ========== CIRCUIT BREAKERS ==========
class CircuitBreaker:
    """Circuit breaker for external services"""
    
    def __init__(self, failure_threshold=5, recovery_timeout=30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = 'closed'  # closed, open, half-open
        
    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        
        if self.failures >= self.failure_threshold:
            self.state = 'open'
            logger.warning(f"Circuit breaker opened after {self.failures} failures")
    
    def record_success(self):
        self.failures = 0
        if self.state == 'half-open':
            self.state = 'closed'
            logger.info("Circuit breaker closed after successful request")
    
    def allow_request(self) -> bool:
        if self.state == 'closed':
            return True
        elif self.state == 'open':
            # Check if recovery timeout has passed
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = 'half-open'
                logger.info("Circuit breaker entering half-open state")
                return True
            return False
        elif self.state == 'half-open':
            # Allow one request to test
            return True
        return False

# ========== APPLICATION STATE ==========
class AppState:
    """Thread-safe application state"""
    
    def __init__(self):
        self.tracked_tokens: Dict[str, TokenState] = {}
        self._tokens_lock = asyncio.Lock()
        self.websocket_connected = False
        self.websocket_task = None
        self.websocket_subscriptions = set()
        self.session: Optional[aiohttp.ClientSession] = None
        self.shutdown_event = asyncio.Event()
        self.start_time = datetime.now()
        self.file_lock = asyncio.Lock()
        self.last_successful_save = None
        
        # Circuit breakers
        self.helius_circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        self.trading_circuit = CircuitBreaker(failure_threshold=2, recovery_timeout=30)
        
        # Performance tracking
        self.stats = {
            'transactions_processed': 0,
            'trades_executed': 0,
            'errors': 0,
            'last_error': None
        }
    
    async def add_token(self, mint: str, token_state: TokenState) -> bool:
        """Thread-safe token addition"""
        async with self._tokens_lock:
            if len(self.tracked_tokens) >= Config.MAX_TRACKED_TOKENS:
                return False
            self.tracked_tokens[mint] = token_state
            Metrics.tracked_tokens.set(len(self.tracked_tokens))
            return True
    
    async def remove_token(self, mint: str) -> bool:
        """Thread-safe token removal"""
        async with self._tokens_lock:
            if mint in self.tracked_tokens:
                del self.tracked_tokens[mint]
                Metrics.tracked_tokens.set(len(self.tracked_tokens))
                return True
            return False
    
    async def get_token(self, mint: str) -> Optional[TokenState]:
        """Thread-safe token retrieval"""
        async with self._tokens_lock:
            return self.tracked_tokens.get(mint)
    
    async def get_all_tokens(self) -> List[TokenState]:
        """Thread-safe get all tokens"""
        async with self._tokens_lock:
            return list(self.tracked_tokens.values())
    
    async def update_token_price(self, mint: str, price: float) -> bool:
        """Thread-safe price update"""
        async with self._tokens_lock:
            if mint in self.tracked_tokens:
                token = self.tracked_tokens[mint]
                token.update_price(price)
                
                # Update metadata if stale
                if (token.metadata.last_verified is None or 
                    (datetime.now() - token.metadata.last_verified) > timedelta(hours=24)):
                    asyncio.create_task(self._refresh_metadata(token))
                
                return True
            return False
    
    async def _refresh_metadata(self, token_state: TokenState):
        """Refresh token metadata in background"""
        try:
            metadata = await price_tracker.get_token_metadata(token_state.mint)
            token_state.metadata = metadata
        except Exception as e:
            logger.debug(f"Failed to refresh metadata for {token_state.mint}: {e}")

app_state = AppState()

# ========== TRANSACTION PROCESSOR ==========
class TransactionProcessor:
    """High-performance transaction processor with backpressure"""
    
    def __init__(self):
        self.processing_queue = asyncio.Queue(maxsize=500)
        self.signature_cache = TTLCache(maxsize=1000, ttl=300)  # 5 minute TTL
        self.rate_limiter = RateLimiter(max_calls=40, period=1)
        self.semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_TASKS)
        self._processor_task = None
        self._active_tasks = set()
        
        # Batch processing
        self.batch_size = 10
        self.batch_timeout = 0.1  # seconds
        
    async def start(self):
        """Start the processor"""
        self._processor_task = asyncio.create_task(self._process_batches())
        logger.info("Transaction processor started")
    
    async def stop(self):
        """Stop the processor gracefully"""
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        
        # Wait for active tasks
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
    
    async def submit(self, signature: str, mentioned_tokens: List[str]):
        """Submit transaction for processing"""
        try:
            # Skip if signature recently processed
            if signature in self.signature_cache:
                return
            
            # Check memory before queuing
            if SystemMonitor.get_memory_usage() > Config.MEMORY_LIMIT_MB * 0.8:
                logger.warning("Memory high, dropping transaction")
                return
            
            # Non-blocking put with timeout
            await asyncio.wait_for(
                self.processing_queue.put((signature, mentioned_tokens)),
                timeout=0.1
            )
            Metrics.queue_size.set(self.processing_queue.qsize())
            
        except asyncio.TimeoutError:
            logger.warning("Processing queue full, dropping transaction")
        except Exception as e:
            logger.error(f"Error submitting transaction: {e}")
    
    async def _process_batches(self):
        """Process transactions in batches for efficiency"""
        while not app_state.shutdown_event.is_set():
            try:
                batch = []
                start_time = time.time()
                
                # Collect batch
                while len(batch) < self.batch_size and (time.time() - start_time) < self.batch_timeout:
                    try:
                        item = await asyncio.wait_for(self.processing_queue.get(), timeout=0.05)
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break
                
                if batch:
                    # Process batch concurrently
                    tasks = [self._process_item(sig, tokens) for sig, tokens in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Mark all as done
                    for _ in batch:
                        self.processing_queue.task_done()
                    
                    Metrics.queue_size.set(self.processing_queue.qsize())
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Batch processing error: {e}")
                await asyncio.sleep(1)
    
    async def _process_item(self, signature: str, mentioned_tokens: List[str]):
        """Process single transaction item"""
        task = asyncio.create_task(self._process_with_retry(signature, mentioned_tokens))
        self._active_tasks.add(task)
        
        try:
            await task
        finally:
            self._active_tasks.discard(task)
    
    @retry(
        stop=stop_after_attempt(Config.MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError))
    )
    async def _process_with_retry(self, signature: str, mentioned_tokens: List[str]):
        """Process transaction with retry logic"""
        async with self.semaphore:
            try:
                with Metrics.transaction_latency.time():
                    # Check circuit breaker
                    if not app_state.helius_circuit.allow_request():
                        logger.warning("Helius circuit breaker open, skipping transaction")
                        return
                    
                    # Fetch transaction
                    tx = await price_tracker.fetch_transaction(signature)
                    
                    if not tx:
                        app_state.helius_circuit.record_failure()
                        return
                    
                    app_state.helius_circuit.record_success()
                    
                    # Parse swap data
                    swap_info = await price_tracker.parse_swap_data(tx, mentioned_tokens)
                    
                    if swap_info:
                        # Update token price
                        token_mint = swap_info['token']
                        price_usd = swap_info['price_usd']
                        
                        if await app_state.update_token_price(token_mint, price_usd):
                            # Check trading conditions
                            token_state = await app_state.get_token(token_mint)
                            if token_state and token_state.status == TokenStatus.TRACKING:
                                await check_trading_conditions(token_state)
                    
                    # Cache signature
                    self.signature_cache[signature] = True
                    Metrics.transactions_processed.inc()
                    app_state.stats['transactions_processed'] += 1
                    
            except Exception as e:
                Metrics.transactions_failed.inc()
                logger.error(f"Transaction processing failed: {e}")
                raise

# ========== RATE LIMITER ==========
class RateLimiter:
    """Token bucket rate limiter"""
    
    def __init__(self, max_calls: int, period: int):
        self.max_calls = max_calls
        self.period = period
        self.tokens = max_calls
        self.updated_at = time.time()
        self.lock = asyncio.Lock()
    
    async def acquire(self):
        async with self.lock:
            now = time.time()
            elapsed = now - self.updated_at
            
            # Add tokens based on elapsed time
            self.tokens += elapsed * (self.max_calls / self.period)
            self.tokens = min(self.tokens, self.max_calls)
            self.updated_at = now
            
            if self.tokens < 1:
                # Calculate sleep time
                sleep_time = (1 - self.tokens) * (self.period / self.max_calls)
                await asyncio.sleep(sleep_time)
                return await self.acquire()  # Retry after sleep
            
            self.tokens -= 1
            return True

# ========== SYSTEM MONITOR ==========
class SystemMonitor:
    """System resource monitoring"""
    
    @staticmethod
    def get_memory_usage() -> float:
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024
    
    @staticmethod
    def get_cpu_usage() -> float:
        return psutil.cpu_percent(interval=0.1)
    
    @staticmethod
    def get_system_stats() -> Dict[str, Any]:
        """Get comprehensive system statistics"""
        process = psutil.Process(os.getpid())
        memory = process.memory_info()
        
        return {
            'memory_rss_mb': memory.rss / 1024 / 1024,
            'memory_vms_mb': memory.vms / 1024 / 1024,
            'cpu_percent': process.cpu_percent(),
            'threads': process.num_threads(),
            'fds': process.num_fds() if hasattr(process, 'num_fds') else None,
            'io_counters': process.io_counters() if hasattr(process, 'io_counters') else None,
            'system_cpu': psutil.cpu_percent(interval=0.1, percpu=True),
            'system_memory': dict(psutil.virtual_memory()._asdict()),
            'disk_usage': dict(psutil.disk_usage(Config.DATA_DIR)._asdict()),
        }
    
    @staticmethod
    def check_health() -> Dict[str, bool]:
        """Perform health checks"""
        checks = {
            'memory_ok': SystemMonitor.get_memory_usage() < Config.MEMORY_LIMIT_MB * Config.HEALTH_MEMORY_THRESHOLD,
            'cpu_ok': SystemMonitor.get_cpu_usage() < Config.HEALTH_CPU_THRESHOLD,
            'disk_ok': psutil.disk_usage(Config.DATA_DIR).percent < 90,
            'process_alive': psutil.pid_exists(os.getpid()),
        }
        
        # Check if we can write to data directory
        try:
            test_file = Config.DATA_DIR / '.health_check'
            test_file.touch()
            test_file.unlink()
            checks['disk_writable'] = True
        except Exception:
            checks['disk_writable'] = False
        
        return checks

# ========== PRICE TRACKER ==========
class PriceTracker:
    """Price tracker with intelligent caching"""
    
    def __init__(self):
        self.token_metadata_cache = TTLCache(maxsize=500, ttl=3600)  # 1 hour TTL
        self.price_cache = TTLCache(maxsize=1000, ttl=30)  # 30 second TTL
        self.sol_price = 100.0
        self.last_sol_price_update = datetime.now() - timedelta(minutes=5)
        self.metadata_session = None
        
    async def initialize(self):
        """Initialize separate session for metadata requests"""
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=10,
            ttl_dns_cache=300,
        )
        self.metadata_session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=5),
        )
    
    async def cleanup(self):
        """Clean up resources"""
        if self.metadata_session:
            await self.metadata_session.close()
    
    async def get_token_metadata(self, mint_address: str) -> TokenMetadata:
        """Get token metadata with caching"""
        # Check cache
        if mint_address in self.token_metadata_cache:
            return self.token_metadata_cache[mint_address]
        
        # Default metadata
        metadata = TokenMetadata(
            mint=mint_address,
            symbol=mint_address[:4] + '...',
            name=mint_address[:8] + '...',
            decimals=6,
            last_verified=datetime.now()
        )
        
        try:
            # Fetch from chain
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [mint_address, {"encoding": "jsonParsed"}]
            }
            
            async with self.metadata_session.post(
                Config.HELIUS_RPC_URL, 
                json=payload,
                headers={'Content-Type': 'application/json'}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('result'):
                        account_info = data['result']['value']
                        if account_info and 'parsed' in account_info:
                            token_info = account_info['parsed']['info']
                            metadata = TokenMetadata(
                                mint=mint_address,
                                symbol=token_info.get('symbol', metadata.symbol),
                                name=token_info.get('name', metadata.name),
                                decimals=token_info.get('decimals', 6),
                                last_verified=datetime.now()
                            )
            
            # Update cache
            self.token_metadata_cache[mint_address] = metadata
            Metrics.cache_size.labels(cache_type='token_metadata').set(len(self.token_metadata_cache))
            
        except Exception as e:
            logger.debug(f"Failed to fetch metadata for {mint_address}: {e}")
        
        return metadata
    
    @backoff.on_exception(
        backoff.expo,
        (aiohttp.ClientError, asyncio.TimeoutError),
        max_tries=3,
        max_time=30
    )
    async def get_sol_price(self) -> float:
        """Get SOL price with exponential backoff"""
        now = datetime.now()
        if (now - self.last_sol_price_update).total_seconds() < 30:
            return self.sol_price
        
        try:
            # Try multiple price sources for redundancy
            sources = [
                "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112",
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                "https://public-api.birdeye.so/public/price?address=So11111111111111111111111111111111111111112"
            ]
            
            for source in sources:
                try:
                    async with app_state.session.get(source, timeout=3) as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            if 'jup.ag' in source:
                                self.sol_price = float(data['data']['So11111111111111111111111111111111111111112']['price'])
                            elif 'coingecko' in source:
                                self.sol_price = float(data['solana']['usd'])
                            elif 'birdeye' in source:
                                self.sol_price = float(data['data']['value'])
                            
                            self.last_sol_price_update = now
                            return self.sol_price
                except Exception:
                    continue
            
            logger.warning("All SOL price sources failed, using cached value")
            
        except Exception as e:
            logger.error(f"Failed to fetch SOL price: {e}")
        
        return self.sol_price
    
    async def parse_swap_data(self, transaction_data: Dict, mentioned_tokens: List[str]) -> Optional[Dict]:
        """Parse swap data from transaction"""
        if not transaction_data:
            return None
        
        try:
            meta = transaction_data.get('meta', {})
            pre_balances = meta.get('preTokenBalances', [])
            post_balances = meta.get('postTokenBalances', [])
            
            # Create balance map
            balances = {}
            for balance in pre_balances + post_balances:
                mint = balance.get('mint')
                if mint in mentioned_tokens or mint in QUOTE_TOKENS:
                    if mint not in balances:
                        balances[mint] = {'pre': 0, 'post': 0}
                    
                    amount = float(balance.get('uiTokenAmount', {}).get('uiAmount', 0))
                    
                    if balance in pre_balances:
                        balances[mint]['pre'] = amount
                    else:
                        balances[mint]['post'] = amount
            
            # Find swaps
            for token_mint in mentioned_tokens:
                if token_mint in balances:
                    token_change = balances[token_mint]['post'] - balances[token_mint]['pre']
                    
                    # Check against quote tokens
                    for quote_mint, quote_symbol in QUOTE_TOKENS.items():
                        if quote_mint in balances:
                            quote_change = balances[quote_mint]['post'] - balances[quote_mint]['pre']
                            
                            # Check if this is a swap (one positive, one negative)
                            if token_change * quote_change < 0:
                                price = abs(quote_change) / abs(token_change)
                                
                                # Convert to USD if needed
                                if quote_mint == "So11111111111111111111111111111111111111112":
                                    sol_price = await self.get_sol_price()
                                    price_usd = price * sol_price
                                else:
                                    price_usd = price
                                
                                return {
                                    'token': token_mint,
                                    'price': price,
                                    'price_usd': price_usd,
                                    'token_amount': abs(token_change),
                                    'quote_amount': abs(quote_change),
                                    'quote_token': quote_symbol,
                                    'timestamp': datetime.now()
                                }
            
            return None
            
        except Exception as e:
            logger.error(f"Error parsing swap data: {e}")
            return None
    
    @circuit(failure_threshold=5, recovery_timeout=60)
    async def fetch_transaction(self, signature: str) -> Optional[Dict]:
        """Fetch transaction with circuit breaker"""
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
        
        try:
            async with app_state.session.post(
                Config.HELIUS_RPC_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("result")
                elif response.status == 429:
                    logger.warning(f"Rate limited by Helius for {signature[:16]}...")
                    raise aiohttp.ClientResponseError(
                        request_info=None,
                        history=None,
                        status=429,
                        message="Rate limited"
                    )
                else:
                    logger.error(f"Helius error {response.status} for {signature[:16]}...")
                    return None
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching transaction {signature[:16]}...")
            raise
        except aiohttp.ClientError as e:
            logger.error(f"Client error fetching transaction: {e}")
            raise

price_tracker = PriceTracker()

# ========== QUOTE TOKENS & DEX PROGRAMS ==========
QUOTE_TOKENS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}

DEX_PROGRAMS = {
    "Raydium_V4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "Raydium_CLMM": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "Orca_Whirlpool": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "Pump_fun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
}

# ========== FILE OPERATIONS ==========
class FileManager:
    """Thread-safe file operations with backup"""
    
    @staticmethod
    async def save_tokens(tokens: Dict[str, TokenState]) -> bool:
        """Save tokens to file with atomic write"""
        temp_file = None
        backup_file = None
        
        try:
            async with app_state.file_lock:
                # Prepare data
                data = {}
                for mint, token in tokens.items():
                    data[mint] = {
                        'metadata': {
                            'symbol': token.metadata.symbol,
                            'name': token.metadata.name,
                            'decimals': token.metadata.decimals,
                            'last_verified': token.metadata.last_verified.isoformat() if token.metadata.last_verified else None,
                        },
                        'initial_price': token.initial_price,
                        'current_price': token.current_price,
                        'status': token.status.value,
                        'last_updated': token.last_updated.isoformat() if token.last_updated else None,
                        'entry_time': token.entry_time.isoformat() if token.entry_time else None,
                        'exit_time': token.exit_time.isoformat() if token.exit_time else None,
                        'quantity': token.quantity,
                        'highest_price': token.highest_price,
                        'lowest_price': token.lowest_price,
                    }
                
                # Create backup of current file
                if Config.TOKENS_FILE.exists():
                    backup_file = Config.BACKUP_DIR / f"tokens_backup_{int(time.time())}.json"
                    import shutil
                    shutil.copy2(Config.TOKENS_FILE, backup_file)
                
                # Write to temporary file
                temp_file = Config.TOKENS_FILE.with_suffix('.tmp')
                async with aiofiles.open(temp_file, 'w') as f:
                    await f.write(json.dumps(data, indent=2, default=str))
                
                # Atomic rename (POSIX)
                os.rename(temp_file, Config.TOKENS_FILE)
                
                # Cleanup old backups (keep last 5)
                await FileManager._cleanup_old_backups()
                
                app_state.last_successful_save = datetime.now()
                logger.info(f"Saved {len(tokens)} tokens to file")
                return True
                
        except Exception as e:
            logger.error(f"Failed to save tokens: {e}")
            
            # Restore from backup if available
            if backup_file and backup_file.exists():
                try:
                    import shutil
                    shutil.copy2(backup_file, Config.TOKENS_FILE)
                    logger.info("Restored from backup")
                except Exception as restore_error:
                    logger.error(f"Failed to restore from backup: {restore_error}")
            
            return False
    
    @staticmethod
    async def load_tokens() -> Dict[str, TokenState]:
        """Load tokens from file"""
        tokens = {}
        
        try:
            if not Config.TOKENS_FILE.exists():
                logger.info("No token file found, starting fresh")
                return tokens
            
            async with aiofiles.open(Config.TOKENS_FILE, 'r') as f:
                content = await f.read()
                if not content.strip():
                    return tokens
                
                data = json.loads(content)
                
                for mint, token_data in data.items():
                    try:
                        metadata = TokenMetadata(
                            mint=mint,
                            symbol=token_data['metadata']['symbol'],
                            name=token_data['metadata']['name'],
                            decimals=token_data['metadata']['decimals'],
                            last_verified=datetime.fromisoformat(token_data['metadata']['last_verified']) 
                                if token_data['metadata']['last_verified'] else None,
                        )
                        
                        token_state = TokenState(
                            mint=mint,
                            metadata=metadata,
                            initial_price=token_data.get('initial_price'),
                            current_price=token_data.get('current_price'),
                            status=TokenStatus(token_data.get('status', 'waiting_for_price')),
                            last_updated=datetime.fromisoformat(token_data['last_updated']) 
                                if token_data.get('last_updated') else None,
                            entry_time=datetime.fromisoformat(token_data['entry_time']) 
                                if token_data.get('entry_time') else None,
                            exit_time=datetime.fromisoformat(token_data['exit_time']) 
                                if token_data.get('exit_time') else None,
                            quantity=token_data.get('quantity'),
                            highest_price=token_data.get('highest_price'),
                            lowest_price=token_data.get('lowest_price'),
                        )
                        
                        tokens[mint] = token_state
                        
                    except Exception as e:
                        logger.error(f"Error loading token {mint}: {e}")
                        continue
            
            logger.info(f"Loaded {len(tokens)} tokens from file")
            
        except Exception as e:
            logger.error(f"Failed to load tokens: {e}")
        
        return tokens
    
    @staticmethod
    async def _cleanup_old_backups():
        """Cleanup old backup files"""
        try:
            backup_files = sorted(Config.BACKUP_DIR.glob('tokens_backup_*.json'))
            
            # Keep last 5 backups
            for old_backup in backup_files[:-5]:
                old_backup.unlink()
                
        except Exception as e:
            logger.error(f"Failed to cleanup backups: {e}")

# ========== TRADING FUNCTIONS ==========
async def check_trading_conditions(token_state: TokenState):
    """Check if trading conditions are met"""
    if token_state.status != TokenStatus.TRACKING:
        return
    
    if not token_state.initial_price or not token_state.current_price:
        return
    
    price_change = (token_state.current_price - token_state.initial_price) / token_state.initial_price
    
    if price_change >= Config.TAKE_PROFIT_PERCENT:
        token_state.status = TokenStatus.EXITED_PROFIT
        logger.info(f"🎯 TAKE PROFIT for {token_state.symbol or token_state.mint[:8]}...: {price_change:+.2%}")
        await execute_sell_order(token_state)
        Metrics.trades_executed.labels(type='sell', status='profit').inc()
        
    elif price_change <= -Config.STOP_LOSS_PERCENT:
        token_state.status = TokenStatus.EXITED_STOPLOSS
        logger.info(f"🚨 STOP LOSS for {token_state.symbol or token_state.mint[:8]}...: {price_change:+.2%}")
        await execute_sell_order(token_state)
        Metrics.trades_executed.labels(type='sell', status='stoploss').inc()

async def execute_buy_order(token_state: TokenState, price_usd: float):
    """Execute buy order with circuit breaker"""
    if not app_state.trading_circuit.allow_request():
        logger.warning("Trading circuit breaker open, skipping buy order")
        return
    
    try:
        url = f"{Config.TRADING_ENDPOINT}/trade/buy"
        payload = {
            "coin_symbol": token_state.symbol,
            "coin_mint": token_state.mint,
            "amount_usdc": price_usd,
            "slippage": 1.0
        }
        headers = {
            "Authorization": f"Bearer {Config.TRADING_AUTH_TOKEN}",
            "Content-Type": "application/json"
        }
        
        async with app_state.session.post(url, json=payload, headers=headers, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                quantity = data.get('quantity')
                
                token_state.quantity = quantity
                token_state.initial_price = price_usd
                token_state.current_price = price_usd
                token_state.status = TokenStatus.TRACKING
                token_state.entry_time = datetime.now()
                token_state.last_updated = datetime.now()
                
                app_state.trading_circuit.record_success()
                Metrics.trades_executed.labels(type='buy', status='success').inc()
                app_state.stats['trades_executed'] += 1
                
                logger.info(f"✅ BUY ORDER for {token_state.symbol or token_state.mint[:8]}... at ${price_usd:.8f}")
                
            else:
                app_state.trading_circuit.record_failure()
                error_text = await response.text()
                logger.error(f"Buy order failed: {response.status} - {error_text}")
                Metrics.trades_executed.labels(type='buy', status='failed').inc()
                
    except Exception as e:
        app_state.trading_circuit.record_failure()
        logger.error(f"Buy order error for {token_state.mint}: {e}")
        Metrics.trades_executed.labels(type='buy', status='error').inc()

async def execute_sell_order(token_state: TokenState):
    """Execute sell order with circuit breaker"""
    if not app_state.trading_circuit.allow_request():
        logger.warning("Trading circuit breaker open, skipping sell order")
        return
    
    try:
        url = f"{Config.TRADING_ENDPOINT}/trade/sell"
        payload = {
            "coin_symbol": token_state.symbol,
            "coin_mint": token_state.mint,
            "percentage": 100.0
        }
        headers = {
            "Authorization": f"Bearer {Config.TRADING_AUTH_TOKEN}",
            "Content-Type": "application/json"
        }
        
        async with app_state.session.post(url, json=payload, headers=headers, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                
                token_state.exit_time = datetime.now()
                app_state.trading_circuit.record_success()
                Metrics.trades_executed.labels(type='sell', status='success').inc()
                app_state.stats['trades_executed'] += 1
                
                logger.info(f"✅ SELL ORDER for {token_state.symbol or token_state.mint[:8]}...")
                
            else:
                app_state.trading_circuit.record_failure()
                error_text = await response.text()
                logger.error(f"Sell order failed: {response.status} - {error_text}")
                Metrics.trades_executed.labels(type='sell', status='failed').inc()
                
    except Exception as e:
        app_state.trading_circuit.record_failure()
        logger.error(f"Sell order error for {token_state.mint}: {e}")
        Metrics.trades_executed.labels(type='sell', status='error').inc()

# ========== WEBSOCKET HANDLER ==========
class WebSocketManager:
    """WebSocket connection manager with reconnection logic"""
    
    def __init__(self):
        self.ws = None
        self.connected = False
        self.subscription_ids = set()
        self.reconnect_delay = Config.WS_RECONNECT_BASE
        self._connection_task = None
    
    async def connect(self):
        """Establish WebSocket connection"""
        while not app_state.shutdown_event.is_set():
            try:
                logger.info(f"Connecting to WebSocket... (attempt delay: {self.reconnect_delay}s)")
                
                async with websockets.connect(
                    Config.HELIUS_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**20  # 1MB max message size
                ) as websocket:
                    self.ws = websocket
                    self.connected = True
                    self.reconnect_delay = Config.WS_RECONNECT_BASE
                    
                    logger.info("✅ WebSocket connected")
                    
                    # Subscribe to tokens
                    await self.subscribe_to_tokens()
                    
                    # Listen for messages
                    await self._listen()
                    
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e.code} - {e.reason}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            
            self.connected = False
            self.ws = None
            
            if not app_state.shutdown_event.is_set():
                await self._handle_reconnect()
    
    async def _listen(self):
        """Listen for WebSocket messages"""
        async for message in self.ws:
            if app_state.shutdown_event.is_set():
                break
            
            # Process message asynchronously
            asyncio.create_task(self._process_message(message))
    
    async def _process_message(self, message: str):
        """Process incoming WebSocket message"""
        try:
            parsed = json.loads(message)
            
            # Track message size
            Metrics.websocket_message_size.observe(len(message))
            
            # Handle subscription confirmation
            if 'result' in parsed and isinstance(parsed['result'], int):
                subscription_id = parsed['result']
                self.subscription_ids.add(subscription_id)
                logger.debug(f"Subscription confirmed: {subscription_id}")
                return
            
            # Handle transaction notification
            if 'params' in parsed and 'result' in parsed['params']:
                result = parsed['params']['result']
                signature = result.get('value', {}).get('signature', '')
                
                if not signature:
                    return
                
                # Extract mentioned tokens from logs
                logs = ' '.join(result.get('value', {}).get('logs', []))
                
                # Check for mentions of our tracked tokens
                mentioned_tokens = []
                async with app_state._tokens_lock:
                    for mint in app_state.tracked_tokens.keys():
                        if mint in logs:
                            mentioned_tokens.append(mint)
                
                if mentioned_tokens:
                    # Submit for processing
                    await transaction_processor.submit(signature, mentioned_tokens)
                    
        except json.JSONDecodeError:
            logger.error("Invalid JSON in WebSocket message")
        except Exception as e:
            logger.error(f"Error processing WebSocket message: {e}")
    
    async def subscribe_to_tokens(self):
        """Subscribe to token mentions"""
        async with app_state._tokens_lock:
            tokens = list(app_state.tracked_tokens.keys())
        
        # Subscribe in batches of 50 (Helius limit)
        batch_size = 50
        for i in range(0, len(tokens), batch_size):
            batch = tokens[i:i + batch_size]
            
            subscription = {
                "jsonrpc": "2.0",
                "id": f"batch_{i//batch_size}",
                "method": "logsSubscribe",
                "params": [
                    {"mentions": batch},  # Subscribe to token mentions
                    {"commitment": "confirmed"}
                ]
            }
            
            try:
                await self.ws.send(json.dumps(subscription))
                logger.debug(f"Subscribed to batch {i//batch_size} ({len(batch)} tokens)")
                await asyncio.sleep(0.1)  # Rate limiting
            except Exception as e:
                logger.error(f"Failed to subscribe to batch {i//batch_size}: {e}")
    
    async def _handle_reconnect(self):
        """Handle reconnection with exponential backoff"""
        Metrics.websocket_reconnects.inc()
        
        logger.info(f"Reconnecting in {self.reconnect_delay:.1f}s...")
        await asyncio.sleep(self.reconnect_delay)
        
        # Exponential backoff with jitter
        self.reconnect_delay = min(
            self.reconnect_delay * 1.5,
            Config.WS_RECONNECT_MAX
        ) * random.uniform(0.9, 1.1)
    
    async def update_subscriptions(self):
        """Update subscriptions when tokens change"""
        if self.connected and self.ws:
            # Unsubscribe from all
            for sub_id in list(self.subscription_ids):
                try:
                    unsubscribe = {
                        "jsonrpc": "2.0",
                        "id": f"unsub_{sub_id}",
                        "method": "logsUnsubscribe",
                        "params": [sub_id]
                    }
                    await self.ws.send(json.dumps(unsubscribe))
                    self.subscription_ids.discard(sub_id)
                except Exception as e:
                    logger.error(f"Failed to unsubscribe {sub_id}: {e}")
            
            # Resubscribe with current tokens
            await self.subscribe_to_tokens()

websocket_manager = WebSocketManager()
transaction_processor = TransactionProcessor()

# ========== BACKGROUND TASKS ==========
async def periodic_save():
    """Periodically save tokens to disk"""
    while not app_state.shutdown_event.is_set():
        try:
            await asyncio.sleep(60)  # Save every minute
            
            async with app_state._tokens_lock:
                tokens = app_state.tracked_tokens.copy()
            
            if tokens:
                await FileManager.save_tokens(tokens)
                
        except Exception as e:
            logger.error(f"Periodic save error: {e}")

async def memory_monitor():
    """Monitor and manage memory usage"""
    while not app_state.shutdown_event.is_set():
        try:
            await asyncio.sleep(Config.MEMORY_CHECK_INTERVAL)
            
            memory_usage = SystemMonitor.get_memory_usage()
            Metrics.memory_usage.set(memory_usage)
            Metrics.cpu_usage.set(SystemMonitor.get_cpu_usage())
            
            # Emergency cleanup at 90% memory
            if memory_usage > Config.MEMORY_LIMIT_MB * 0.9:
                logger.warning(f"🚨 High memory usage: {memory_usage:.1f}MB")
                
                # Force garbage collection
                gc.collect()
                
                # Clear caches
                price_tracker.token_metadata_cache.clear()
                price_tracker.price_cache.clear()
                transaction_processor.signature_cache.clear()
                
                # Drop old tokens if still high
                if memory_usage > Config.MEMORY_LIMIT_MB * 0.95:
                    await _emergency_memory_cleanup()
            
            # Update cache metrics
            Metrics.cache_size.labels(cache_type='signature').set(len(transaction_processor.signature_cache))
            Metrics.cache_size.labels(cache_type='metadata').set(len(price_tracker.token_metadata_cache))
            
        except Exception as e:
            logger.error(f"Memory monitor error: {e}")

async def _emergency_memory_cleanup():
    """Emergency memory cleanup"""
    try:
        # Remove tokens that haven't been updated in 24 hours
        cutoff = datetime.now() - timedelta(hours=24)
        tokens_to_remove = []
        
        async with app_state._tokens_lock:
            for mint, token in app_state.tracked_tokens.items():
                if token.last_updated and token.last_updated < cutoff:
                    tokens_to_remove.append(mint)
            
            for mint in tokens_to_remove[:20]:  # Remove at most 20
                del app_state.tracked_tokens[mint]
                logger.warning(f"Removed old token {mint[:8]}... for memory cleanup")
        
        Metrics.tracked_tokens.set(len(app_state.tracked_tokens))
        
    except Exception as e:
        logger.error(f"Emergency cleanup error: {e}")

async def health_checker():
    """Periodic health checks"""
    while not app_state.shutdown_event.is_set():
        try:
            await asyncio.sleep(30)
            
            checks = SystemMonitor.check_health()
            
            # Log any failed checks
            for check_name, passed in checks.items():
                if not passed:
                    logger.warning(f"Health check failed: {check_name}")
                    
        except Exception as e:
            logger.error(f"Health checker error: {e}")

# ========== LIFESPAN MANAGER ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager"""
    logger.info("🚀 Starting Token Tracker v3.0")
    logger.info(f"Instance: {socket.gethostname()}")
    logger.info(f"Memory limit: {Config.MEMORY_LIMIT_MB}MB")
    logger.info(f"Max tokens: {Config.MAX_TRACKED_TOKENS}")
    
    # Start memory tracing if enabled
    if Config.ENABLE_MEMORY_MONITOR:
        tracemalloc.start(10)
    
    # Initialize HTTP session
    connector = aiohttp.TCPConnector(
        limit=100,
        limit_per_host=50,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        force_close=True,
    )
    
    timeout = aiohttp.ClientTimeout(
        total=Config.REQUEST_TIMEOUT,
        connect=5,
        sock_read=10,
        sock_connect=5,
    )
    
    app_state.session = aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={'User-Agent': f'TokenTracker/3.0 ({socket.gethostname()})'}
    )
    
    # Initialize price tracker
    await price_tracker.initialize()
    
    # Load saved tokens
    tokens = await FileManager.load_tokens()
    for mint, token_state in tokens.items():
        await app_state.add_token(mint, token_state)
    
    # Start transaction processor
    await transaction_processor.start()
    
    # Start background tasks
    background_tasks = [
        asyncio.create_task(websocket_manager.connect()),
        asyncio.create_task(periodic_save()),
        asyncio.create_task(memory_monitor()),
        asyncio.create_task(health_checker()),
    ]
    
    # Update metrics
    Metrics.tracked_tokens.set(len(tokens))
    
    logger.info(f"✅ Application started with {len(tokens)} tokens")
    
    yield
    
    # Shutdown sequence
    logger.info("🛑 Shutting down...")
    app_state.shutdown_event.set()
    
    # Stop all tasks
    for task in background_tasks:
        task.cancel()
    
    # Graceful shutdown
    await asyncio.gather(*background_tasks, return_exceptions=True)
    
    # Stop transaction processor
    await transaction_processor.stop()
    
    # Cleanup
    await price_tracker.cleanup()
    
    if app_state.session:
        await app_state.session.close()
    
    # Final save
    async with app_state._tokens_lock:
        await FileManager.save_tokens(app_state.tracked_tokens)
    
    # Memory report
    if Config.ENABLE_MEMORY_MONITOR:
        snapshot = tracemalloc.take_snapshot()
        top_stats = snapshot.statistics('lineno')[:5]
        logger.info("Top memory allocations:")
        for stat in top_stats:
            logger.info(f"  {stat}")
        tracemalloc.stop()
    
    logger.info("✅ Shutdown complete")

# ========== FASTAPI APP ==========
app = FastAPI(
    title="Solana Token Tracker API v3",
    description="Production-ready Solana token tracking with automated trading",
    version="3.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv('CORS_ORIGINS', '*').split(','),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== API ROUTES ==========
@app.get("/")
async def root():
    """Root endpoint with basic info"""
    uptime = datetime.now() - app_state.start_time
    memory = SystemMonitor.get_memory_usage()
    
    return {
        "service": "Solana Token Tracker",
        "version": "3.0.0",
        "status": "operational",
        "instance": socket.gethostname(),
        "uptime_seconds": uptime.total_seconds(),
        "tracked_tokens": len(app_state.tracked_tokens),
        "memory_usage_mb": round(memory, 2),
        "websocket_connected": websocket_manager.connected,
    }

@app.post("/tokens", response_model=TokenResponse)
async def add_token(request: AddTokenRequest, background_tasks: BackgroundTasks):
    """Add a new token to track"""
    # Check token limit
    async with app_state._tokens_lock:
        if len(app_state.tracked_tokens) >= Config.MAX_TRACKED_TOKENS:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum token limit ({Config.MAX_TRACKED_TOKENS}) reached"
            )
    
    # Validate token exists
    metadata = await price_tracker.get_token_metadata(request.mint_address)
    
    # Create token state
    token_state = TokenState(
        mint=request.mint_address,
        metadata=metadata,
        status=TokenStatus.WAITING_FOR_PRICE,
    )
    
    if request.initial_price_usd:
        token_state.initial_price = request.initial_price_usd
        token_state.current_price = request.initial_price_usd
        token_state.status = TokenStatus.TRACKING
        token_state.entry_time = datetime.now()
        
        # Execute buy order in background
        background_tasks.add_task(execute_buy_order, token_state, request.initial_price_usd)
    
    # Add to tracking
    added = await app_state.add_token(request.mint_address, token_state)
    if not added:
        raise HTTPException(status_code=400, detail="Failed to add token")
    
    # Update WebSocket subscriptions
    if websocket_manager.connected:
        asyncio.create_task(websocket_manager.update_subscriptions())
    
    Metrics.tokens_added.inc()
    
    logger.info(f"Added token: {metadata.symbol} ({request.mint_address[:8]}...)")
    
    return TokenResponse(
        mint=token_state.mint,
        symbol=token_state.symbol,
        name=token_state.metadata.name,
        status=token_state.status,
        initial_price=token_state.initial_price,
        current_price=token_state.current_price,
        profit_loss_percent=token_state.profit_loss_percent,
        highest_price=token_state.highest_price,
        lowest_price=token_state.lowest_price,
        last_updated=token_state.last_updated,
        entry_time=token_state.entry_time,
        exit_time=token_state.exit_time,
    )

@app.get("/tokens", response_model=List[TokenResponse])
async def get_tokens():
    """Get all tracked tokens"""
    tokens = await app_state.get_all_tokens()
    
    return [
        TokenResponse(
            mint=token.mint,
            symbol=token.symbol,
            name=token.metadata.name,
            status=token.status,
            initial_price=token.initial_price,
            current_price=token.current_price,
            profit_loss_percent=token.profit_loss_percent,
            highest_price=token.highest_price,
            lowest_price=token.lowest_price,
            last_updated=token.last_updated,
            entry_time=token.entry_time,
            exit_time=token.exit_time,
        )
        for token in tokens
    ]

@app.get("/tokens/{mint}", response_model=TokenResponse)
async def get_token(mint: str):
    """Get specific token"""
    token_state = await app_state.get_token(mint)
    if not token_state:
        raise HTTPException(status_code=404, detail="Token not found")
    
    return TokenResponse(
        mint=token_state.mint,
        symbol=token_state.symbol,
        name=token_state.metadata.name,
        status=token_state.status,
        initial_price=token_state.initial_price,
        current_price=token_state.current_price,
        profit_loss_percent=token_state.profit_loss_percent,
        highest_price=token_state.highest_price,
        lowest_price=token_state.lowest_price,
        last_updated=token_state.last_updated,
        entry_time=token_state.entry_time,
        exit_time=token_state.exit_time,
    )

@app.delete("/tokens/{mint}")
async def remove_token(mint: str):
    """Remove token from tracking"""
    removed = await app_state.remove_token(mint)
    if not removed:
        raise HTTPException(status_code=404, detail="Token not found")
    
    # Update WebSocket subscriptions
    if websocket_manager.connected:
        asyncio.create_task(websocket_manager.update_subscriptions())
    
    Metrics.tokens_removed.inc()
    
    logger.info(f"Removed token: {mint[:8]}...")
    
    return {"message": f"Token {mint[:8]}... removed"}

@app.post("/tokens/{mint}/sell")
async def sell_token(mint: str):
    """Manually sell a token"""
    token_state = await app_state.get_token(mint)
    if not token_state:
        raise HTTPException(status_code=404, detail="Token not found")
    
    if token_state.status != TokenStatus.TRACKING:
        raise HTTPException(status_code=400, detail="Token is not being tracked")
    
    await execute_sell_order(token_state)
    
    return {"message": f"Sell order executed for {token_state.symbol or mint[:8]}..."}

@app.get("/health", response_model=HealthStatus)
async def health_check():
    """Comprehensive health check endpoint"""
    checks = SystemMonitor.check_health()
    
    # Determine overall status
    if all(checks.values()):
        status = "healthy"
    elif any(key in checks for key in ['memory_ok', 'cpu_ok', 'disk_writable']) and checks.get('memory_ok') and checks.get('disk_writable'):
        status = "degraded"
    else:
        status = "unhealthy"
    
    # Get queue size
    queue_size = transaction_processor.processing_queue.qsize()
    
    return HealthStatus(
        status=status,
        checks=checks,
        websocket_connected=websocket_manager.connected,
        tracked_tokens_count=len(app_state.tracked_tokens),
        memory_usage_mb=round(SystemMonitor.get_memory_usage(), 2),
        cpu_percent=round(SystemMonitor.get_cpu_usage(), 2),
        active_tasks=len(transaction_processor._active_tasks),
        queue_size=queue_size,
        uptime_seconds=round((datetime.now() - app_state.start_time).total_seconds(), 2),
        timestamp=datetime.now(),
    )

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    # Update dynamic metrics
    Metrics.active_tasks.set(len(transaction_processor._active_tasks))
    Metrics.queue_size.set(transaction_processor.processing_queue.qsize())
    
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain"
    )

@app.get("/stats")
async def stats():
    """Application statistics"""
    system_stats = SystemMonitor.get_system_stats()
    
    return {
        "application": {
            "uptime_seconds": (datetime.now() - app_state.start_time).total_seconds(),
            "tracked_tokens": len(app_state.tracked_tokens),
            "websocket_connected": websocket_manager.connected,
            "circuit_breakers": {
                "helius": app_state.helius_circuit.state,
                "trading": app_state.trading_circuit.state,
            },
            "last_successful_save": app_state.last_successful_save,
        },
        "performance": {
            "transactions_processed": app_state.stats['transactions_processed'],
            "trades_executed": app_state.stats['trades_executed'],
            "errors": app_state.stats['errors'],
        },
        "system": system_stats,
    }

@app.get("/debug/memory")
async def debug_memory():
    """Debug memory information"""
    if not Config.ENABLE_MEMORY_MONITOR:
        return {"error": "Memory monitoring not enabled"}
    
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics('lineno')[:10]
    
    return {
        "memory_usage_mb": SystemMonitor.get_memory_usage(),
        "top_allocations": [
            {
                "filename": str(stat.traceback[0].filename),
                "line": stat.traceback[0].lineno,
                "size_kb": stat.size / 1024,
                "count": stat.count,
            }
            for stat in top_stats
        ]
    }

# ========== ERROR HANDLING ==========
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    app_state.stats['errors'] += 1
    app_state.stats['last_error'] = str(exc)
    
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )

# ========== SIGNAL HANDLERS ==========
def handle_signal(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    app_state.shutdown_event.set()

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ========== MAIN ENTRY POINT ==========
if __name__ == "__main__":
    # Validate configuration
    required_vars = ['HELIUS_API_KEY', 'TRADING_AUTH_TOKEN']
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        logger.error(f"Missing required environment variables: {missing}")
        sys.exit(1)
    
    # Configure uvicorn
    uvicorn_config = {
        "host": os.getenv('HOST', '0.0.0.0'),
        "port": int(os.getenv('PORT', 8080)),
        "log_config": None,  # Use our own logging
        "access_log": False,
        "timeout_keep_alive": 30,
        "limit_concurrency": 1000,
        "limit_max_requests": 10000,
        "workers": int(os.getenv('UVICORN_WORKERS', 1)),
    }
    
    # Run application
    uvicorn.run("__main__:app", **uvicorn_config)
