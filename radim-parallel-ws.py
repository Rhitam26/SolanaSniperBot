import asyncio
import json
import base64
from typing import Dict, Callable, Optional
from dataclasses import dataclass
from solana.rpc.websocket_api import connect
from solders.pubkey import Pubkey
from datetime import datetime
import aiohttp

@dataclass
class PoolConfig:
    name: str
    pool_address: str
    base_decimals: int
    quote_decimals: int
    base_vault: str  # Base token vault address
    quote_vault: str  # Quote token vault address

class RaydiumMultiPriceTracker:
    def __init__(self, rpc_ws_url="wss://api.mainnet-beta.solana.com", rpc_http_url="https://api.mainnet-beta.solana.com"):
        self.rpc_ws_url = rpc_ws_url
        self.rpc_http_url = rpc_http_url
        self.pools: Dict[str, PoolConfig] = {}
        self.prices: Dict[str, float] = {}
        self.price_callbacks: list[Callable] = []
        self.websocket = None
        
    def add_pool(self, name: str, pool_address: str, base_decimals: int = 9, 
                 quote_decimals: int = 6, base_vault: str = "", quote_vault: str = ""):
        """Add a pool to track"""
        self.pools[pool_address] = PoolConfig(
            name=name,
            pool_address=pool_address,
            base_decimals=base_decimals,
            quote_decimals=quote_decimals,
            base_vault=base_vault,
            quote_vault=quote_vault
        )
        print(f"✅ Added pool: {name}")
    
    def add_price_callback(self, callback: Callable):
        """Add callback function to be called on price updates"""
        self.price_callbacks.append(callback)
    
    async def fetch_pool_info(self, pool_address: str):
        """Fetch pool information from Raydium"""
        async with aiohttp.ClientSession() as session:
            try:
                # Fetch from Raydium API
                url = f"https://api.raydium.io/v2/ammV3/ammPools"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Find specific pool
                        for pool in data.get('data', []):
                            if pool.get('id') == pool_address:
                                return pool
            except Exception as e:
                print(f"Error fetching pool info: {e}")
        return None
    
    async def get_token_balance(self, session, vault_address: str) -> Optional[int]:
        """Get token balance from vault account"""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountBalance",
                "params": [vault_address]
            }
            
            async with session.post(self.rpc_http_url, json=payload) as response:
                data = await response.json()
                if 'result' in data and 'value' in data['result']:
                    return int(data['result']['value']['amount'])
        except Exception as e:
            print(f"Error getting balance for {vault_address}: {e}")
        return None
    
    async def calculate_price_from_vaults(self, pool_config: PoolConfig):
        """Calculate price by fetching vault balances directly"""
        async with aiohttp.ClientSession() as session:
            try:
                # Get both vault balances
                base_balance = await self.get_token_balance(session, pool_config.base_vault)
                quote_balance = await self.get_token_balance(session, pool_config.quote_vault)
                
                if base_balance and quote_balance:
                    base_amount = base_balance / (10 ** pool_config.base_decimals)
                    quote_amount = quote_balance / (10 ** pool_config.quote_decimals)
                    
                    if base_amount > 0:
                        return quote_amount / base_amount
            except Exception as e:
                print(f"Error calculating price for {pool_config.name}: {e}")
        return None
    
    async def subscribe_to_vault(self, pool_config: PoolConfig):
        """Subscribe to vault account changes for real-time updates"""
        try:
            # Subscribe to both vaults
            await self.websocket.account_subscribe(
                Pubkey.from_string(pool_config.base_vault),
                commitment="confirmed",
                encoding="base64"
            )
            await self.websocket.account_subscribe(
                Pubkey.from_string(pool_config.quote_vault),
                commitment="confirmed",
                encoding="base64"
            )
            print(f"✅ Subscribed to vaults for {pool_config.name}")
            
        except Exception as e:
            print(f"❌ Error subscribing to vaults for {pool_config.name}: {e}")
    
    async def process_account_update(self, pubkey: str, data: bytes):
        """Process account update and calculate new price"""
        try:
            # Find which pool this vault belongs to
            for pool_address, pool_config in self.pools.items():
                if pubkey in [pool_config.base_vault, pool_config.quote_vault]:
                    # Recalculate price
                    price = await self.calculate_price_from_vaults(pool_config)
                    
                    if price:
                        old_price = self.prices.get(pool_config.name)
                        self.prices[pool_config.name] = price
                        
                        # Calculate price change
                        change = ""
                        if old_price:
                            pct_change = ((price - old_price) / old_price) * 100
                            change = f"({pct_change:+.2f}%)"
                        
                        timestamp = datetime.now().strftime("%H:%M:%S")
                        print(f"[{timestamp}] 💰 {pool_config.name}: ${price:.8f} {change}")
                        
                        # Call callbacks
                        for callback in self.price_callbacks:
                            await callback(pool_config.name, price)
                    
                    break
                    
        except Exception as e:
            print(f"Error processing account update: {e}")
    
    async def initial_price_fetch(self):
        """Fetch initial prices for all pools"""
        print("\n📊 Fetching initial prices...\n")
        tasks = []
        for pool_config in self.pools.values():
            tasks.append(self.calculate_price_from_vaults(pool_config))
        
        prices = await asyncio.gather(*tasks)
        
        for pool_config, price in zip(self.pools.values(), prices):
            if price:
                self.prices[pool_config.name] = price
                print(f"💰 {pool_config.name}: ${price:.8f}")
    
    async def track_prices(self):
        """Main tracking loop"""
        async with connect(self.rpc_ws_url) as websocket:
            self.websocket = websocket
            
            # Fetch initial prices
            await self.initial_price_fetch()
            
            print("\n🎯 Starting real-time tracking... (Ctrl+C to stop)\n")
            
            # Subscribe to all vault accounts
            for pool_config in self.pools.values():
                await self.subscribe_to_vault(pool_config)
            
            # Listen for updates
            first = await websocket.recv()
            subscription_id = first[0].result
            
            async for msg in websocket:
                try:
                    if hasattr(msg[0], 'result'):
                        result = msg[0].result
                        if hasattr(result, 'value'):
                            # Get the account pubkey and data
                            account_data = result.value.data
                            # Process the update
                            # Note: We need to track which subscription corresponds to which vault
                            # For simplicity, we'll recalculate prices periodically
                            pass
                            
                except Exception as e:
                    print(f"Error in main loop: {e}")
                    continue
    
    async def periodic_price_update(self, interval: int = 1):
        """Alternative: Poll prices periodically instead of WebSocket"""
        print("\n📊 Starting periodic price updates...\n")
        
        # Fetch initial prices
        await self.initial_price_fetch()
        
        print(f"\n🎯 Updating every {interval} second(s)... (Ctrl+C to stop)\n")
        
        while True:
            try:
                tasks = []
                for pool_config in self.pools.values():
                    tasks.append(self.calculate_price_from_vaults(pool_config))
                
                prices = await asyncio.gather(*tasks)
                
                for pool_config, price in zip(self.pools.values(), prices):
                    if price:
                        old_price = self.prices.get(pool_config.name)
                        self.prices[pool_config.name] = price
                        
                        change = ""
                        if old_price and old_price != price:
                            pct_change = ((price - old_price) / old_price) * 100
                            change = f"({pct_change:+.2f}%)"
                        
                        if change:  # Only print if price changed
                            timestamp = datetime.now().strftime("%H:%M:%S")
                            print(f"[{timestamp}] 💰 {pool_config.name}: ${price:.8f} {change}")
                        
                        # Call callbacks
                        for callback in self.price_callbacks:
                            await callback(pool_config.name, price)
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                print(f"Error in periodic update: {e}")
                await asyncio.sleep(interval)
    
    def get_price(self, token_name: str) -> Optional[float]:
        """Get current price for a token"""
        return self.prices.get(token_name)
    
    def get_all_prices(self) -> Dict[str, float]:
        """Get all current prices"""
        return self.prices.copy()


# Example callback for your trading bot
async def price_update_callback(token_name: str, price: float):
    """This is where you'd implement your trading logic"""
    # Example: Check if price crosses certain thresholds
    # if token_name == "SOL/USDC" and price > 100:
    #     await execute_trade()
    pass


async def main():
    # Initialize tracker
    tracker = RaydiumMultiPriceTracker()
    
    # Add callback
    tracker.add_price_callback(price_update_callback)
    
    # Add pools - Here are some popular Raydium pools
    # Format: (name, pool_id, base_decimals, quote_decimals, base_vault, quote_vault)
    
    pools_to_track = [
        # SOL/USDC
        ("SOL/USDC", "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2", 9, 6,
         "DQyrAcCrDXQ7NeoqGgDCZwBvWDcYmFCjSb9JtteuvPpz",  # SOL vault
         "HLmqeL62xR1QoZ1HKKbXRrdN1p3phKpxRMb2VVopvBBz"),  # USDC vault
        
        # Add your 20 pools here with their vault addresses
        # You can find these on Raydium's website or using their API
        
        # Example format for more pools:
        # ("RAY/USDC", "POOL_ADDRESS", 6, 6, "BASE_VAULT", "QUOTE_VAULT"),
        # ("BONK/SOL", "POOL_ADDRESS", 5, 9, "BASE_VAULT", "QUOTE_VAULT"),
        # etc...
    ]
    
    for pool_data in pools_to_track:
        tracker.add_pool(*pool_data)
    
    # Choose tracking method:
    # Method 1: Periodic polling (More reliable, easier to implement)
    await tracker.periodic_price_update(interval=1)  # Update every second
    
    # Method 2: WebSocket (Real-time but more complex)
    # await tracker.track_prices()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Stopped tracking")
