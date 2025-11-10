import aiohttp
import asyncio
import re

async def search_pyth_feeds(search_terms):
    """Search for feeds by symbol name"""
    url = "https://hermes.pyth.network/v2/price_feeds"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                feeds = await response.json()
                
                results = {}
                for feed in feeds:
                    attributes = feed.get("attributes", {})
                    symbol = attributes.get("symbol", "")
                    
                    for term in search_terms:
                        # Use regex for more flexible matching
                        pattern = re.compile(f"^{term.upper()}/", re.IGNORECASE)
                        if pattern.match(symbol) or term.upper() in symbol.upper():
                            results[term] = {
                                "feed_id": feed.get("id"),
                                "symbol": symbol,
                                "base": attributes.get("base"),
                                "quote": attributes.get("quote_currency"),
                                "market": attributes.get("market")
                            }
                            break
                
                return results
            return {}

async def main():
    # Search for these tokens
    tokens_to_find = ["COMMUNITY"]
    
    print("🔍 Searching Pyth Network for feed IDs...")
    results = await search_pyth_feeds(tokens_to_find)
    
    print("\n🎯 Found Feed IDs:")
    print("=" * 50)
    
    for token, info in results.items():
        print(f"💰 {token}:")
        print(f"   Symbol: {info['symbol']}")
        print(f"   Feed ID: {info['feed_id']}")
        print(f"   Market: {info.get('market', 'N/A')}")
        print()

if __name__ == "__main__":
    asyncio.run(main())