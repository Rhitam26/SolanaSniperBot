import requests
import json

# 3. DexScreener API - Search token (No API key required)
def get_token_from_dexscreener(query):
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
        return None


# # 4. Helius API - Get token metadata (Requires API key)
# def get_token_metadata_helius(mint_address, api_key):
#     """
#     Get token metadata from Helius
#     Args:
#         mint_address: Token contract address
#         api_key: Your Helius API key
#     """
#     try:
#         url = f'https://api.helius.xyz/v0/token-metadata?api-key={api_key}'
#         payload = {
#             "mintAccounts": [mint_address]
#         }
#         headers = {'Content-Type': 'application/json'}
        
#         response = requests.post(url, json=payload, headers=headers)
#         response.raise_for_status()
        
#         data = response.json()
        
#         if data and len(data) > 0:
#             token = data[0]
#             print(f"Token: {token['onChainMetadata']['metadata']['data']['name']}")
#             print(f"Symbol: {token['onChainMetadata']['metadata']['data']['symbol']}")
#             print(f"Contract Address: {token['account']}")
#             return token['account']
#         else:
#             print("No metadata found")
#             return None
            
#     except Exception as e:
#         print(f"Error fetching from Helius: {e}")
#         return None


# # 5. Birdeye API - Get token list (Requires API key)
# def get_token_from_birdeye(api_key, symbol=None):
#     """
#     Get token from Birdeye token list
#     Args:
#         api_key: Your Birdeye API key
#         symbol: Optional token symbol to search for
#     """
#     try:
#         url = 'https://public-api.birdeye.so/public/tokenlist?chain=solana'
#         headers = {'X-API-KEY': api_key}
        
#         response = requests.get(url, headers=headers)
#         response.raise_for_status()
        
#         data = response.json()
        
#         if symbol:
#             # Search for specific token
#             tokens = data.get('data', {}).get('tokens', [])
#             for token in tokens:
#                 if token.get('symbol', '').upper() == symbol.upper():
#                     print(f"Token: {token.get('name', 'N/A')}")
#                     print(f"Symbol: {token['symbol']}")
#                     print(f"Contract Address: {token['address']}")
#                     return token['address']
#             print(f"Token '{symbol}' not found")
#             return None
#         else:
#             print(f"Total tokens: {len(data.get('data', {}).get('tokens', []))}")
#             return data
            
#     except Exception as e:
#         print(f"Error fetching from Birdeye: {e}")
#         return None


# Example usage
if __name__ == "__main__":

    
    print("\n" + "=" * 50)
    print("DexScreener API - Search for SOL")
    print("=" * 50)
    get_token_from_dexscreener('anoncoin')
