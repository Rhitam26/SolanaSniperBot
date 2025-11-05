import asyncio
import aiohttp

async def quick_get_mint(token_symbol: str) -> str:
    """Quick function to get mint address"""
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.raydium.io/v2/main/pairs") as response:
            data = await response.json()
            for pair in data:
                name = pair.get('name', '')
                if token_symbol.upper() in name.upper():
                    if name.split('/')[0].strip().upper() == token_symbol.upper():
                        return pair.get('baseMint')
                    elif '/' in name and name.split('/')[1].strip().upper() == token_symbol.upper():
                        return pair.get('quoteMint')
    return None

# Usage
mint = asyncio.run(quick_get_mint('SOL'))
print(f"SOL Mint: {mint}")
