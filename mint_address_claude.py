import requests

# Use the new Jupiter Token API V2 endpoint
JUPITER_TOKEN_SEARCH_URL = "https://lite-api.jup.ag/tokens/v2/search"

def get_mints_by_symbol(symbol: str, case_insensitive: bool = True, include_unverified: bool = True, timeout: int = 20):
    """
    Returns a list of dicts for tokens matching the given symbol on Solana mainnet.
    Each dict includes: mint, symbol, name, verified, and decimals.
    Uses Jupiter Token API V2.
    """
    try:
        # Search for the token by symbol using the new API
        params = {'query': symbol}
        resp = requests.get(JUPITER_TOKEN_SEARCH_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        
        # The API returns an array of token objects
        if not data:
            return []
        
        # Filter matches based on symbol (API already does fuzzy search)
        matches = []
        for token in data:
            token_symbol = token.get("symbol", "")
            
            # Check if symbol matches
            if case_insensitive:
                if token_symbol.upper() == symbol.upper():
                    matches.append(token)
            else:
                if token_symbol == symbol:
                    matches.append(token)
        
        # Optionally filter out unverified entries
        if not include_unverified:
            matches = [t for t in matches if t.get("isVerified", False)]
        
        # Sort so verified entries appear first
        matches.sort(key=lambda t: 1 if t.get("isVerified", False) else 0, reverse=True)
        
        # Map to a clean output format
        result = [
            {
                "mint": t.get("id"),  # V2 uses "id" for mint address
                "symbol": t.get("symbol"),
                "name": t.get("name"),
                "verified": bool(t.get("isVerified", False)),
                "decimals": t.get("decimals"),
            }
            for t in matches
        ]
        return result
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching tokens: {e}")
        return []

if __name__ == "__main__":
    # Example usage
    symbol = "wobbles"  # replace with your token symbol
    mints = get_mints_by_symbol(symbol, case_insensitive=True, include_unverified=True)
    
    if not mints:
        print(f"No matches found for symbol: {symbol}")
    else:
        for i, t in enumerate(mints, start=1):
            print(f"{i}. {t['symbol']} ({t['name']}) | mint: {t['mint']} | verified: {t['verified']} | decimals: {t['decimals']}")