import requests

ALL_POOLS_URL = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"  # as used by community reference :contentReference[oaicite:1]{index=1}

def fetch_all_pools():
    resp = requests.get(ALL_POOLS_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    pools = data.get("official", []) + data.get("unOfficial", [])
    return pools

def find_pool_for_mints(base_mint: str, quote_mint: str):
    pools = fetch_all_pools()
    for pool in pools:
        # each pool item typically has something like mintA, mintB (or baseMint / quoteMint) keys
        mA = pool.get("baseMint") or pool.get("mintA")
        mB = pool.get("quoteMint") or pool.get("mintB")
        if not mA or not mB:
            continue
        # check both orders
        if {mA, mB} == {base_mint, quote_mint}:
            return pool
    return None

if __name__ == "__main__":
    base = "So11111111111111111111111111111111111111112"  
    quote = "9yZ5Ru8pbmJZ6Q2DKLCGXkaLNwkm83cnJ4QCw4PFpump"  
    pool = find_pool_for_mints(base, quote)
    if pool:
        print("Found pool:", pool)
    else:
        print("Pool not found for mints:", base, quote)
