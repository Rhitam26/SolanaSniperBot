import asyncio
import websockets
import json
import aiohttp

class BirdeyeWebSocketMonitor:
    def __init__(self, api_key=None):
        self.ws_url = "wss://public-api.birdeye.so/ws"
        self.api_key = api_key  # Optional for public endpoints
        self.subscribed_tokens = set()
        
        # Common Solana meme coin mint addresses
        self.meme_coins = {
            "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
            "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", 
            "POPCAT": "7GCygD6ykmrkqkA9F6c3p8C8XUo9Dk7HWcY6o7cXW6oW",
            "MYRO": "myroWe6eDq6RHqXZcjg8YF2pumF5pZ7q8n1vCjvqk7W",
            "WEN": "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk",
            "SAMO": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
            "HARAMBE": "HARAMBE7kH2upg3pC1tqoPxo5bSdnRz2wRNpfaqD1p3E",
            "COQ": "coqonS8mdHdk9h5fE54N3eL3KXyZqRnGcN53N5TH8jN",
            "NOS": "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7",
            "GME": "GMEeNwkVCiMzpCkUANoop6PSJqveqpejQ2woUyTKydey",
            "TRUMP": "5wietqneyiQrqyiFQfqod5hqUpfgNY6M76Y71R1Y3ebx",
        }
    
    async def subscribe_to_tokens(self, websocket, tokens):
        """Subscribe to multiple tokens"""
        for token_name in tokens:
            if token_name in self.meme_coins:
                mint_address = self.meme_coins[token_name]
                
                subscribe_message = {
                    "method": "subscribe",
                    "params": {
                        "id": token_name,
                        "type": "price",
                        "address": mint_address
                    }
                }
                
                await websocket.send(json.dumps(subscribe_message))
                self.subscribed_tokens.add(token_name)
                print(f"✅ Subscribed to {token_name}: {mint_address[:8]}...")
    
    async def handle_price_update(self, data):
        """Process price update messages"""
        try:
            token_name = data.get("id")
            result = data.get("result", {})
            
            if result:
                price = result.get("value")
                update_type = result.get("updateType", "unknown")
                volume = result.get("volume24h", {})
                
                if price and price > 0:
                    print(f"💰 {token_name}: ${price:.8f}")
                    print(f"   📊 Update: {update_type}")
                    
                    if volume:
                        vol_usd = volume.get('unified', 0)
                        if vol_usd:
                            print(f"   📈 24h Volume: ${vol_usd:,.2f}")
                    
                    # Additional metrics if available
                    price_change_24h = result.get("priceChange24hPercent")
                    if price_change_24h:
                        change_emoji = "🟢" if price_change_24h > 0 else "🔴"
                        print(f"   {change_emoji} 24h Change: {price_change_24h:+.2f}%")
                    
                    print()  # Empty line for readability
                
        except Exception as e:
            print(f"⚠️ Error processing update: {e}")
    
    async def start_monitoring(self, tokens_to_monitor=None):
        """Start WebSocket monitoring"""
        if tokens_to_monitor is None:
            tokens_to_monitor = list(self.meme_coins.keys())[:5]  # Limit to 5 by default
        
        print("🚀 Starting Birdeye WebSocket Monitor")
        print("=" * 50)
        print(f"📊 Monitoring {len(tokens_to_monitor)} tokens")
        print()
        
        try:
            async with websockets.connect(self.ws_url) as websocket:
                # Subscribe to tokens
                await self.subscribe_to_tokens(websocket, tokens_to_monitor)
                
                # Listen for messages
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        
                        # Handle subscription confirmation
                        if data.get("result") == "subscribed":
                            print(f"🎯 Successfully subscribed to {data.get('id')}")
                        
                        # Handle price updates
                        elif data.get("method") == "price_update":
                            await self.handle_price_update(data)
                        
                        # Handle other message types
                        else:
                            print(f"📨 Other message: {data}")
                            
                    except json.JSONDecodeError:
                        print("⚠️ Invalid JSON received")
                    except Exception as e:
                        print(f"⚠️ Error processing message: {e}")
                        
        except websockets.exceptions.ConnectionClosed:
            print("❌ Connection closed. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
            await self.start_monitoring(tokens_to_monitor)
        except Exception as e:
            print(f"❌ Connection error: {e}")
            print("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
            await self.start_monitoring(tokens_to_monitor)

# Advanced version with more features
class AdvancedBirdeyeMonitor(BirdeyeWebSocketMonitor):
    def __init__(self, api_key=None):
        super().__init__(api_key)
        self.price_history = {}
        self.alert_thresholds = {}
    
    async def set_price_alert(self, token_name, threshold_price, direction="above"):
        """Set price alerts for tokens"""
        self.alert_thresholds[token_name] = {
            "price": threshold_price,
            "direction": direction
        }
        print(f"🔔 Price alert set for {token_name}: {direction} ${threshold_price:.8f}")
    
    async def check_alerts(self, token_name, current_price):
        """Check if price triggers any alerts"""
        if token_name in self.alert_thresholds:
            alert = self.alert_thresholds[token_name]
            threshold = alert["price"]
            direction = alert["direction"]
            
            triggered = False
            if direction == "above" and current_price >= threshold:
                triggered = True
            elif direction == "below" and current_price <= threshold:
                triggered = True
            
            if triggered:
                print(f"🚨 ALERT! {token_name} is {direction} ${threshold:.8f}")
                print(f"   Current price: ${current_price:.8f}")
                # You could add notification logic here (email, telegram, etc.)
    
    async def handle_price_update(self, data):
        """Enhanced price update handler with alerts"""
        try:
            token_name = data.get("id")
            result = data.get("result", {})
            
            if result:
                price = result.get("value")
                update_type = result.get("updateType", "unknown")
                
                if price and price > 0:
                    # Store price history
                    if token_name not in self.price_history:
                        self.price_history[token_name] = []
                    
                    self.price_history[token_name].append({
                        "price": price,
                        "timestamp": asyncio.get_event_loop().time()
                    })
                    
                    # Keep only last 100 prices
                    if len(self.price_history[token_name]) > 100:
                        self.price_history[token_name].pop(0)
                    
                    # Display update
                    print(f"💰 {token_name}: ${price:.8f}")
                    print(f"   📊 Update: {update_type}")
                    
                    # Check alerts
                    await self.check_alerts(token_name, price)
                    
                    # Calculate simple moving average if we have history
                    history = self.price_history[token_name]
                    if len(history) >= 5:
                        recent_prices = [p["price"] for p in history[-5:]]
                        sma = sum(recent_prices) / len(recent_prices)
                        trend = "↗️" if price > sma else "↘️"
                        print(f"   {trend} 5-period SMA: ${sma:.8f}")
                    
                    print()
                
        except Exception as e:
            print(f"⚠️ Error processing update: {e}")

# Usage examples
async def main():
    # Basic monitoring
    monitor = BirdeyeWebSocketMonitor()
    
    # Monitor specific tokens
    tokens_to_watch = ["WIF", "BONK", "POPCAT", "MYRO", "WEN"]
    
    # Advanced monitoring with alerts
    advanced_monitor = AdvancedBirdeyeMonitor()
    
    # Set some price alerts
    await advanced_monitor.set_price_alert("BONK", 0.000025, "above")
    await advanced_monitor.set_price_alert("WIF", 2.50, "below")
    
    print("Choose monitoring mode:")
    print("1. Basic monitoring")
    print("2. Advanced monitoring with alerts")
    
    choice = input("Enter choice (1 or 2): ").strip()
    
    if choice == "2":
        await advanced_monitor.start_monitoring(tokens_to_watch)
    else:
        await monitor.start_monitoring(tokens_to_watch)

async def quick_start():
    """Quick start with default settings"""
    monitor = BirdeyeWebSocketMonitor()
    await monitor.start_monitoring(["WIF", "BONK", "POPCAT"])

if __name__ == "__main__":
    # For quick testing
    asyncio.run(quick_start())
    
    # For full features
    # asyncio.run(main())