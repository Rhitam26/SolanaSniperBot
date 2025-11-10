from fastapi import FastAPI, HTTPException, Query
import requests
import os

app = FastAPI(title="Jupiter Order Fetcher", description="Fetch AMM key and label for given token pair", version="1.0")

BASE_URL = "https://lite-api.jup.ag/ultra/v1/order"

@app.get("/get_order")
def get_order(
    inputMint: str = Query(..., description="Input mint address"),
    outputMint: str = Query(..., description="Output mint address"),
    amount: int = Query(100, description="Amount in token units"),
    slippageBps: int = Query(1, description="Slippage in basis points"),
):
    """
    Fetch Jupiter order details and return ammKey and label.
    """

    try:
        # Build request URL
        url = f"{BASE_URL}?inputMint={inputMint}&outputMint={outputMint}&amount={amount}&slippageBps={slippageBps}"

        # Send GET request
        response = requests.get(url, timeout=10)

        # Raise error for non-200 responses
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Jupiter API error: {response.text}")

        data = response.json()

        # Check if routePlan and swapInfo exist
        if "routePlan" not in data or not data["routePlan"]:
            raise HTTPException(status_code=404, detail="No route plan found for given mint pair.")

        swap_info = data["routePlan"][0].get("swapInfo", {})
        if not swap_info:
            raise HTTPException(status_code=500, detail="Swap information not found in response.")

        amm_key = swap_info.get("ammKey")
        label = swap_info.get("label")

        if not amm_key or not label:
            raise HTTPException(status_code=500, detail="Incomplete data: ammKey or label missing.")

        # Return required fields
        return {"ammKey": amm_key, "label": label}

    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Request to Jupiter API timed out.")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error contacting Jupiter API: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
