import requests
import json

async def get_token_from_dexscreener(query):
    """
    Search token on DexScreener
    Args:
        query: Token symbol or name
    """
    try:
        url = f'https://api.dexscreener.com/latest/dex/search/?q={query}'
        response = requests.get(url)
        response.raise_for_status()
        
        data = response.json()
        
        if data.get('pairs') and len(data['pairs']) > 0:
            pair = data['pairs'][0]
            token = pair['baseToken']
            print(f"Token: {token['name']}")
            print(f"Symbol: {token['symbol']}")
            print(f"Contract Address: {token['address']}")
            print(f"Price USD: ${pair.get('priceUsd', 'N/A')}")
            return token['address']
        else:
            print(f"No results found for '{query}'")
            return None
            
    except Exception as e:
        print(f"Error fetching from DexScreener: {e}")
        return {
"error": str(e)
        }

if __name__ == "__main__":
    import asyncio
    query = "TRUMP"
    asyncio.run(get_token_from_dexscreener(query))
