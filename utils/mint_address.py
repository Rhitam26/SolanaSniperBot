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
        pairs= data['pairs']
        
        if data.get('pairs') and len(data['pairs']) > 0:
            pair = data['pairs'][0]

            token = pair['baseToken']
            name = token['name']
            symbol = token['symbol']
            address = token['address']
            price_usd = pair.get('priceUsd', 'N/A')
            print(type(pair.get('priceUsd', 'N/A')))
            response ={
                "symbol": symbol,
                "address": address,
                "price_usd": price_usd
            }
            print( f"Found token on DexScreener: {name} ({symbol}) at address {address} with price ${price_usd}")
            return response
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
    query = "ERROR"
    asyncio.run(get_token_from_dexscreener(query))
