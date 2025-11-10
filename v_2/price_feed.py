import aiohttp
import asyncio

# --- Pyth API Endpoint ---
PYTH_FEED_LOOKUP = "https://hermes.pyth.network/api/latest_price_feeds"

# --- Coins to search ---
COINS = ["SOL"]

async def get_usd_feed_ids(symbols):
    """Fetch feed IDs from Pyth for given coin symbols quoted in USD"""
    async with aiohttp.ClientSession() as session:
        async with session.get(PYTH_FEED_LOOKUP) as resp:
            if resp.status != 200:
                print(f"Failed to fetch data from Pyth API. Status: {resp.status}")
                return {}

            data = await resp.json()

    feed_map = {}
    for feed in data:
        attrs = feed.get("attributes", {})
        base = attrs.get("base")
        quote = attrs.get("quote_currency")
        feed_id = feed.get("id")

        if quote == "USD" and base in symbols:
            feed_map[base] = feed_id

    return feed_map


async def main():
    print("🔍 Fetching Pyth feed IDs quoted in USD...\n")
    feed_ids = await get_usd_feed_ids(COINS)

    if not feed_ids:
        print("❌ No USD feeds found for given coins.")
        return

    print("✅ Found Feed IDs:\n")
    for coin, fid in feed_ids.items():
        print(f"{coin} → {fid}")


if __name__ == "__main__":
    asyncio.run(main())
