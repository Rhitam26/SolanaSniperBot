import asyncio
import websockets
import json
from datetime import datetime
import traceback

# Optional Redis: uncomment imports if you use RedisCache
# import redis.asyncio as aioredis

class InMemoryCache:
    """Simple in-process cache to store initial prices and state."""
    def __init__(self):
        # structure: {symbol: {"initial_price": float, "state": "active" or "sold/exited"}}
        self._store = {}

    async def set_initial(self, symbol, price):
        if symbol not in self._store:
            self._store[symbol] = {"initial_price": price, "state": "active"}

    async def get_initial(self, symbol):
        item = self._store.get(symbol)
        return item["initial_price"] if item else None
    async def set_state(self, symbol, state):
        if symbol in self._store:
            self._store[symbol]["state"] = state
    async def remove(self, symbol):
        self._store.pop(symbol, None)

    async def has(self, symbol):
        return symbol in self._store

# Redis-backed cache (optional). Uncomment and install redis package to use.
"""
class RedisCache:
    def __init__(self, url="redis://localhost:6379/0"):
        import redis.asyncio as aioredis
        self._r = aioredis.from_url(url, decode_responses=True)

    async def set_initial(self, symbol, price):
        # store initial price and state
        key = f"pricecache:{symbol}"
        exists = await self._r.exists(key)
        if not exists:
            await self._r.hset(key, mapping={"initial_price": price, "state": "active"})

    async def get_initial(self, symbol):
        key = f"pricecache:{symbol}"
        data = await self._r.hgetall(key)
        if data and "initial_price" in data:
            return float(data["initial_price"])
        return None

    async def set_state(self, symbol, state):
        key = f"pricecache:{symbol}"
        await self._r.hset(key, "state", state)

    async def remove(self, symbol):
        key = f"pricecache:{symbol}"
        await self._r.delete(key)

    async def has(self, symbol):
        key = f"pricecache:{symbol}"
        return await self._r.exists(key) == 1
"""

class SolanaPriceTracker:
    def __init__(self, use_redis=False):
        # Track 4 Solana ecosystem coins
        self.coins = ['KITKAT', 'POPKITKAT', 'PIPPIN', 'BUBBLE']
        self.prices = {}           # latest price data
        self.previous_prices = {}  # previous tick
        # Choose cache
        if use_redis:
            raise RuntimeError("RedisCache example is included but not enabled by default. See comments in code.")
            # self.cache = RedisCache()   # if you uncomment RedisCache and want to use it
        else:
            self.cache = InMemoryCache()

        # threshold in percent
        self.threshold_pct = 30.0

    async def connect_binance_websocket(self):
        """Connect to Binance WebSocket for real-time price updates"""
        # Binance WebSocket streams for multiple coins (USDT pairs)
        streams = ['gaymanusdt@ticker']
        stream_url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Connecting to Binance WebSocket...")
        while True:
            try:
                async with websockets.connect(stream_url) as websocket:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Connected successfully!")
                    print("-" * 80)

                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            await self.process_price_update(data)
                        except Exception as e:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error processing message: {e}")
                            traceback.print_exc()
            except websockets.exceptions.WebSocketException as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] WebSocket error: {e}")
                print("Attempting to reconnect in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Unexpected connection error: {e}")
                traceback.print_exc()
                await asyncio.sleep(5)

    async def process_price_update(self, data):
        """Process incoming price data from WebSocket"""
        if 'data' in data:
            ticker = data['data']

            # Extract relevant information
            symbol = ticker['s'].replace('USDT', '')
            try:
                current_price = float(ticker['c'])
            except Exception:
                # if parse error, skip
                return

            price_change_24h = float(ticker.get('p', 0.0))
            initial_price_change = current_price - initial_price if (initial_price := await self.cache.get_initial(symbol)) is not None else 0.0
            price_change_percent = float(ticker.get('P', 0.0))
            high_24h = float(ticker.get('h', 0.0))
            low_24h = float(ticker.get('l', 0.0))
            volume_24h = float(ticker.get('v', 0.0))

            # If we have no initial price for this coin, set it in cache
            initial = await self.cache.get_initial(symbol)
            if initial is None:
                await self.cache.set_initial(symbol, current_price)
                initial = current_price
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Initial price set for {symbol}: {initial}")

            # Determine trend
            trend = self.get_trend(symbol, current_price)

            # Store price data
            self.prices[symbol] = {
                'price': current_price,
                'initial_price': initial,
                'initial_change': initial_price_change,
                'change_24h': price_change_24h,
                'change_percent': price_change_percent,
                'high_24h': high_24h,
                'low_24h': low_24h,
                'volume_24h': volume_24h,
                'trend': trend,
                'timestamp': datetime.now().isoformat()
            }

            # Evaluate thresholds relative to initial price
            await self.evaluate_actions(symbol, initial, current_price)

            # Print price update (only if still tracked)
            if symbol in self.prices:
                self.print_price_update(symbol)

    def get_trend(self, symbol, current_price):
        """Determine if price is trending up or down"""
        if symbol in self.previous_prices:
            if current_price > self.previous_prices[symbol]:
                trend = '↑ UP'
            elif current_price < self.previous_prices[symbol]:
                trend = '↓ DOWN'
            else:
                trend = '→ STABLE'
        else:
            trend = '→ STABLE'

        self.previous_prices[symbol] = current_price
        return trend

    async def evaluate_actions(self, symbol, initial_price, current_price):
        """If price moves +30% or -30% relative to initial, act."""
        try:
            # Avoid division by zero
            if initial_price == 0:
                return

            pct_change_from_initial = ((current_price - initial_price) / initial_price) * 100.0

            # SELL condition: price is > +30% of initial price
            if pct_change_from_initial >= self.threshold_pct:
                timestamp = datetime.now().strftime('%H:%M:%S')
                print(f"[{timestamp}] >>> {symbol} has risen {pct_change_from_initial:.2f}% from initial {initial_price:.8f}. ACTION: SELL")
                # perform any additional sell logic here
                await self.cleanup_after_action(symbol, action="SELL")
                return

            # EXIT condition: price has slipped below -30% from initial price
            if pct_change_from_initial <= -self.threshold_pct:
                timestamp = datetime.now().strftime('%H:%M:%S')
                print(f"[{timestamp}] >>> {symbol} has fallen {pct_change_from_initial:.2f}% from initial {initial_price:.8f}. ACTION: EXIT")
                # perform any additional exit logic here
                await self.cleanup_after_action(symbol, action="EXIT")
                return

            # otherwise no action
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error evaluating actions for {symbol}: {e}")
            traceback.print_exc()

    async def cleanup_after_action(self, symbol, action):
        """Remove coin from cache & internal dicts after SELL/EXIT"""
        # you can expand this to call trade APIs, record audit logs, etc.
        await self.cache.set_state(symbol, action)
        await self.cache.remove(symbol)

        # remove from in-memory trackers
        self.prices.pop(symbol, None)
        self.previous_prices.pop(symbol, None)

        timestamp = datetime.now().strftime('%H:%M:%S')
        print(f"[{timestamp}] {symbol} removed from cache and tracking after {action}.")

    def print_price_update(self, symbol):
        """Print formatted price information"""
        data = self.prices[symbol]
        timestamp = datetime.now().strftime('%H:%M:%S')

        # Format price based on coin
        if symbol == 'BONK':
            price_str = f"${data['price']:.8f}"
        elif symbol == 'USDC':
            price_str = f"${data['price']:.4f}"
        else:
            price_str = f"${data['price']:.2f}"

        # Color indicator for change
        change_indicator = "+" if data['change_percent'] >= 0 else ""

        print(f"[{timestamp}] {symbol:6} | Price: {price_str:12} | "
              f"24h Change: {change_indicator}{data['change_percent']:6.2f}% | "
              f"Trend: {data['trend']:8} | "
              f"Volume: ${data['volume_24h']:,.0f}")

    def get_all_prices(self):
        """Return all current prices (useful for API endpoints)"""
        return self.prices

    def get_price(self, symbol):
        """Get price for a specific coin"""
        return self.prices.get(symbol, None)

async def main():
    """Main function to run the price tracker"""
    print("=" * 80)
    print("SOLANA PRICE TRACKER - Real-time WebSocket Streaming WITH CACHE")
    print("=" * 80)
    print()

    # Choose implementation:
    # Option 1: In-memory cache (default, simplest)
    tracker = SolanaPriceTracker(use_redis=False)
    await tracker.connect_binance_websocket()

    # Option 2: Redis cache (uncomment and configure RedisCache above)
    # tracker = SolanaPriceTracker(use_redis=True)
    # await tracker.connect_binance_websocket()
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nPrice tracker stopped by user.")
    except Exception as e:
        print(f"\nError: {e}")
        traceback.print_exc()
