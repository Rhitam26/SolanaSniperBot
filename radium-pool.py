import asyncio
import aiohttp

async def find_raydium_pools(token_symbols: list):
    """Find Raydium pool addresses for given token pairs"""
    async with aiohttp.ClientSession() as session:
        try:
            url = "https://api.raydium.io/v2/main/pairs"
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    print("\n🔍 Found Raydium Pools:\n")
                    
                    for symbol in token_symbols:
                        matching_pools = [p for p in data if symbol.upper() in p.get('name', '').upper()]
                        
                        for pool in matching_pools[:3]:  # Show top 3 matches
                            print(f"Name: {pool.get('name')}")
                            print(f"Pool ID: {pool.get('ammId')}")
                            print(f"Liquidity: ${pool.get('liquidity', 0):,.2f}")
                            print("-" * 60)
        except Exception as e:
            print(f"Error: {e}")

# Example usage
asyncio.run(find_raydium_pools([
    "SOL/USDC", "RAY/USDC", "BONK/SOL", "JUP/SOL", "WIF/SOL"
]))
