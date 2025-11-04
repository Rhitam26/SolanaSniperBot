import requests

JUPITER_TOKEN_LIST_URL = "https://token.jup.ag/all "

def get_mints_by_symbol(
symbol: str,
case_insensitive: bool = True,
include_unverified: bool = True,
timeout: int = 20
):
"""
Returns a list of dicts for tokens matching the given symbol on Solana mainnet.
Each dict includes: mint, symbol, name, verified, and decimals.
"""
resp = requests.get(JUPITER_TOKEN_LIST_URL, timeout=timeout)
resp.raise_for_status()
data = resp.json()

if case_insensitive:
matches = [t for t in data if (t.get("symbol") or "").upper() == symbol.upper()]
else:
matches = [t for t in data if t.get("symbol") == symbol]

# Optionally filter out unverified entries
if not include_unverified:
matches = [t for t in matches if t.get("verified")]

# Sort so verified entries appear first
matches.sort(key=lambda t: 1 if t.get("verified") else 0, reverse=True)

# Map to a clean output format
result = [
{
"mint": t.get("mintAddress"),
"symbol": t.get("symbol"),
"name": t.get("name"),
"verified": bool(t.get("verified")),
"decimals": t.get("decimals"),
}
for t in matches
]
return result
if name == "main":
# Example usage
symbol = "USDC" # replace with your token symbol
mints = get_mints_by_symbol(symbol, case_insensitive=True, include_unverified=True)
if not mints:
print(f"No matches found for symbol: {symbol}")
else:
for i, t in enumerate(mints, start=1):
print(f"{i}. {t['symbol']} ({t['name']}) | mint: {t['mint']} | verified: {t['verified']} | decimals: {t['decimals']}")
