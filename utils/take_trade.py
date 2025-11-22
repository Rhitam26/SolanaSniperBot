from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field
from typing import Optional, Dict, List
import base58
import base64
import os
from datetime import datetime

# Solana imports
from solders.keypair import Keypair
from solana.rpc.api import Client
from solana.transaction import Transaction
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address, create_associated_token_account
from solders.pubkey import Pubkey
from solders.message import to_bytes_versioned
from dotenv import load_dotenv

load_dotenv()
# Import our trading classes
import requests
import json

app = FastAPI(
    title="Solana Meme Coin Trader API",
    description="API for trading Solana meme coins with Telegram notifications",
    version="1.0.0"
)

# Security
security = HTTPBearer()

# Configuration from environment variables
class Config:
    def __init__(self):
        self.private_key = os.getenv('SOLANA_PRIVATE_KEY')
        self.telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = '1678865548'
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.api_key = '23265688'
        self.jupiter_api_key =  os.getenv('JUPITER_API_KEY')
        
        # Initialize wallet
        if self.private_key:
            private_key_bytes = base58.b58decode(self.private_key)
            self.wallet = Keypair.from_bytes(private_key_bytes)
            self.sender_pubkey = str(self.wallet.pubkey())
        else:
            self.wallet = None

config = Config()

# Pydantic Models
class TradeRequest(BaseModel):
    coin_symbol: str = Field(..., description="Symbol of the meme coin (e.g., BONK)")
    coin_mint: str = Field(..., description="Mint address of the token")
    amount_usdc: float = Field(..., gt=0, description="Amount of USDC to spend")
    slippage: float = Field(10.0, description="Slippage percentage")

class SellRequest(BaseModel):
    coin_symbol: str = Field(..., description="Symbol of the meme coin to sell")
    coin_mint: str = Field(..., description="Mint address of the token")
    percentage: float = Field(100.0, le=100, ge=0, description="Percentage of holdings to sell")

class BalanceRequest(BaseModel):
    coin_mint: str = Field(..., description="Mint address of the token")

class CoinInfo(BaseModel):
    symbol: str
    mint_address: str
    enabled: bool

class TradeResponse(BaseModel):
    success: bool
    message: str
    transaction_hash: Optional[str] = None
    timestamp: str
    quantity : str

class PortfolioResponse(BaseModel):
    sol_balance: float
    usdc_balance: float
    token_balances: Dict[str, float]
    total_estimated_value: Optional[float] = None

class AlertRequest(BaseModel):
    message: str
    priority: str = "info"

# Reuse the trading classes from previous code (with minor adjustments)
class SolanaTrader:
    def __init__(self, wallet, rpc_url="https://api.mainnet-beta.solana.com"):
        self.wallet = wallet
        self.sender_pubkey = str(self.wallet.pubkey())
        self.client = Client(rpc_url)
        
    def get_token_balance(self, token_mint: str):
        """Get balance of a specific token"""
        try:
            token_mint_pubkey = Pubkey.from_string(token_mint)
            associated_token_address = get_associated_token_address(
                self.wallet.pubkey(), token_mint_pubkey
            )
            balance_response = self.client.get_token_account_balance(associated_token_address)
            if balance_response.value:
                return float(balance_response.value.amount) / 10**balance_response.value.decimals
            return 0
        except:
            return 0
    
    def get_sol_balance(self):
        """Get SOL balance"""
        token_mint_pubkey = Pubkey.from_string('BjcRmwm8e25RgjkyaFE56fc7bxRgGPw96JUkXRJFEroT')
        response = self.client.get_token_supply(token_mint_pubkey)
        decimals = response.value.decimals
        print(f"Token has {decimals} decimals")

        balance_response = self.client.get_balance(self.wallet.pubkey())
        return balance_response.value / 10**9
    
    def get_usdc_balance(self):
        """Get USDC balance on Mainnet"""
        try:
            # Mainnet USDC mint address
            usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            balance = self.get_token_balance(usdc_mint)
            print(f"USDC balance check - Mint: {usdc_mint}, Balance: {balance}")
            return balance
        except Exception as e:
            print(f"Error in get_usdc_balance: {e}")
            return 0

    def create_swap_transaction(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Create a swap transaction using Jupiter API"""
        try:
            # Get quote from Jupiter
            jupiter_url = "https://lite-api.jup.ag/ultra/v1/order"
            params = {
                "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC on Mainnet
                "outputMint": token_mint,
                "amount": 1000000,  # Assuming USDC has 6 decimals
                "taker": self.sender_pubkey
            }
            headers = {"Accept": "application/json"}
            
            response = requests.get(jupiter_url, params=params, headers=headers)
            quote_data = response.json()
            print(f"Quote data: {quote_data}")
            if "routePlan" not in quote_data:
                raise HTTPException(status_code=500, detail="No route found for the swap")
            transaction_base64 = quote_data['transaction']
            transaction_bytes = base64.b64decode(transaction_base64)
            transaction = VersionedTransaction.from_bytes(transaction_bytes)

            signature = self.wallet.sign_message(to_bytes_versioned(transaction.message))
              # Create a new signed transaction with the signature
            signed_transaction = VersionedTransaction.populate(transaction.message,[signature])
            signed_transaction_b64 = base64.b64encode(bytes(signed_transaction)).decode('utf-8')
            print("Transaction signed successfully!")
            print(f"Expected output: {quote_data.get('outAmount')} tokens")

            # Get swap transaction
            payload = {
            "requestId": quote_data['requestId'],
            "signedTransaction": signed_transaction_b64
            }
            swap_url = "https://lite-api.jup.ag/ultra/v1/execute"
            swap_response = requests.post(swap_url, json=payload, headers=headers)
            swap_transaction_data = swap_response.json()
            # print("EXECUTED !!!")
            # print(swap_transaction_data['swapEvents'])
            # print(quote_data.get('outAmount'))

            return {
                'result': swap_transaction_data,
                'expected_output': quote_data.get('outAmount'),
                'swap_events' : swap_transaction_data['swapEvents']
            }
        
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error creating swap transaction: {e}")
    
    def execute_swap(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Execute a swap transaction"""
        try:
            swap_transaction = self.create_swap_transaction(token_mint, amount_usdc, slippage)
            print("Got Swap Transaction details..")
            if not swap_transaction:
                return False, "Failed to create swap transaction"
            result = swap_transaction
            return True, result
        except Exception as e:
            return False, str(e)
    
    def buy_meme_coin(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Buy meme coin with USDC"""
        return self.execute_swap(token_mint, amount_usdc, slippage)
    
    def sell_meme_coin(self, token_mint: str):
        """Sell meme coin for USDC"""
        try:
            balance = self.get_token_balance(token_mint)
            print(balance)
            if balance <= 0:
                return False, "No balance to sell"
            
            print("Taker is :",self.sender_pubkey)
            token_mint_pubkey = Pubkey.from_string(token_mint)
            response = self.client.get_token_supply(token_mint_pubkey)
            decimals = response.value.decimals
            print("OUTPUT AMOUNT ",balance)
            print("DECIMALS: ",decimals)
            amount = int(float(balance)*float(10**decimals))
            # Get quote for sellingå
            jupiter_url = "https://lite-api.jup.ag/ultra/v1/order"
            
            params = {
                "inputMint": str(token_mint),
                "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC on Mainnet
                "amount": int(amount),  # Assuming 6 decimals
                "taker": self.sender_pubkey,  
            }
            
            response = requests.get(jupiter_url, params=params)
            quote_data = response.json()
            print(quote_data)
            if "routePlan" not in quote_data:
                raise HTTPException(status_code=500, detail="No route found for the swap")
            transaction_base64 = quote_data['transaction']
            transaction_bytes = base64.b64decode(transaction_base64)
            transaction = VersionedTransaction.from_bytes(transaction_bytes)
            signature = self.wallet.sign_message(to_bytes_versioned(transaction.message))
            signed_transaction = VersionedTransaction.populate(transaction.message,[signature])
            signed_transaction_b64 = base64.b64encode(bytes(signed_transaction)).decode('utf-8')
            print("Transaction signed successfully!")
            print(f"Expected output: {quote_data.get('outAmount')} USDC")
            
            # Execute swap
            swap_url = "https://lite-api.jup.ag/ultra/v1/execute"

            payload = {
            "requestId": quote_data['requestId'],
            "signedTransaction": signed_transaction_b64}

            swap_response = requests.post(swap_url, json=payload)
            swap_transaction_data = swap_response.json()
            print("===================================")
            print(swap_transaction_data)
            return True, {
                'result': str(swap_transaction_data),
                'expected_output': quote_data.get('outAmount')
            }

        except Exception as e:
            return False, str(e)

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
    
    def send_message(self, message: str):
        """Send message via Telegram bot"""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            response = requests.post(url, data=data)
            return response.status_code == 200
        except Exception as e:
            print(f"Failed to send Telegram message: {e}")
            return False
    
    def send_trade_alert(self, action: str, token: str, amount: float, tx_hash: str = None):
        """Send trade notification"""
        message = f"🚀 <b>Trade Executed</b> 🚀\n"
        message += f"Action: {action}\n"
        message += f"Token: {token}\n"
        message += f"Amount: {amount}\n"
        if tx_hash:
            message += f"TX: <code>{tx_hash}</code>\n"
        message += f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        return self.send_message(message)

class MemeCoinTrader:
    def __init__(self, private_key: str, telegram_bot_token: str, telegram_chat_id: str, rpc_url: str):
        private_key_bytes = base58.b58decode(private_key)
        wallet = Keypair.from_bytes(private_key_bytes)
        self.trader = SolanaTrader(wallet, rpc_url)
        self.notifier = TelegramNotifier(telegram_bot_token, telegram_chat_id)
        self.client = Client(rpc_url)

    def get_token_decimals_from_swap_events(self,swap_events, output_mint):
        """Extract decimals from swap events by finding the matching output mint"""
        for event in swap_events:
            if event.get('outputMint') == output_mint:
                # Estimate decimals based on the output amount
                output_amount = int(event.get('outputAmount', 0))
                if output_amount > 0:
                    # This is a heuristic - we assume the actual value should be reasonable
                    # If output amount is very large (> 1e9), it's likely a low decimal token
                    # If output amount is moderate, it's likely a standard decimal token
                    if output_amount > 1e12:
                        return 0  # Very low decimal token
                    elif output_amount > 1e9:
                        return 6  # Medium decimal token
                    else:
                        return 9  # Standard 9 decimal token
        return 6  # Default to 6 if cannot determine

    def calculate_actual_quantity(self,output_amount_result, swap_events, output_mint):
        """Calculate actual quantity received by determining the correct decimal places"""
        try:
            # First try to get decimals from swap events
            decimals = self.get_token_decimals_from_swap_events(swap_events, output_mint)
            
            # Convert to actual quantity
            actual_quantity = int(output_amount_result) / (10 ** decimals)
            
            print(f"Quantity calculation: raw={output_amount_result}, decimals={decimals}, actual={actual_quantity}")
            return actual_quantity
        except Exception as e:
            print(f"Error calculating quantity: {e}")
            # Fallback: try common decimal values
            for decimals in [6, 9, 0, 2]:
                try:
                    quantity = int(output_amount_result) / (10 ** decimals)
                    if 0.0001 <= quantity <= 1000000000:  # Reasonable range for token quantity
                        print(f"Used fallback decimals {decimals}, quantity: {quantity}")
                        return quantity
                except:
                    continue
            return float(output_amount_result)  # Last resort return raw value
  
    def buy_with_alert(self, coin_symbol: str, coin_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Buy meme coin and send notification"""
        try:
            # Validate mint address format
            Pubkey.from_string(coin_mint)
            
            success, result = self.trader.buy_meme_coin(coin_mint, amount_usdc, slippage)
            print("*****",type(result['result']))
            result = result['result']
            # result = json.loads(result)
            print(f"Buy result: success={success}, result={str(result)}")

            if success and isinstance(result, dict):
                # Extract quantity from the swap result
                output_amount_result = result.get('totalOutputAmount', '0')
                swap_events = result.get('swapEvents', [])
                
                # Calculate actual quantity received
                # quantity_received = self.calculate_actual_quantity(output_amount_result, swap_events, coin_mint)
                token_mint_pubkey = Pubkey.from_string(coin_mint)
                response = self.client.get_token_supply(token_mint_pubkey)
                decimals = response.value.decimals
                print("OUTPUT AMOUNT ",output_amount_result)
                print("DECIMALS: ",decimals)
                quantity_received = str(float(output_amount_result)/float(10**decimals))

                print("QUANTITY RECIEVED : ",str(quantity_received))
                result['quantity'] = str(quantity_received)
            if success:
                self.notifier.send_trade_alert(
                    f"BUY {coin_symbol}",
                    coin_symbol,
                    f"{amount_usdc} USDC",
                    result
                )
                return True, result, coin_symbol
            else:
                self.notifier.send_message(f"❌ Buy failed for {coin_symbol}: {result}")
                return False, result, coin_symbol
                
        except Exception as e:
            error_msg = f"❌ Buy error for {coin_symbol}: {str(e)}"
            self.notifier.send_message(error_msg)
            return False, str(e), coin_symbol
    
    def sell_with_alert(self, coin_symbol: str, coin_mint: str, percentage: float = 100.0):
        """Sell meme coin and send notification"""
        try:
            # Validate mint address format
            Pubkey.from_string(coin_mint)
            print("The meme coin address to sell is ", coin_mint)
            success, result = self.trader.sell_meme_coin(coin_mint)
            amount= float(result['expected_output'])/float(10**6)
            
            if success:
                self.notifier.send_trade_alert(
                    f"SELL {coin_symbol}",
                    coin_symbol,
                    f"{percentage}%",
                    result
                )
                return True, str(result), coin_symbol
            else:
                self.notifier.send_message(f"❌ Sell failed for {coin_symbol}: {result}")
                return False, result, coin_symbol, amount
                
        except Exception as e:
            error_msg = f"❌ Sell error for {coin_symbol}: {str(e)}"
            self.notifier.send_message(error_msg)
            return False, str(e), coin_symbol, 0
    
    def get_portfolio(self):
        """Get complete portfolio including SOL, USDC, and all tokens"""
        # sol_balance = self.trader.get_sol_balance()
        # print(f"SOL Balance: {sol_balance}")
        usdc_balance = self.trader.get_usdc_balance()
        print(f"USDC Balance: {usdc_balance}")
        
        return usdc_balance
    
    def get_token_balance(self, coin_mint: str):
        """Get balance for a specific token mint"""
        try:
            Pubkey.from_string(coin_mint)
            return self.trader.get_token_balance(coin_mint)
        except Exception as e:
            print(f"Error getting balance: {e}")
            return 0
        

# Initialize trader
trader = MemeCoinTrader(
    private_key=config.private_key,
    telegram_bot_token=config.telegram_bot_token,
    telegram_chat_id=config.telegram_chat_id,
    rpc_url=config.rpc_url
)

# API Key Dependency
def verify_api_key(credentials: HTTPBearer = Depends(security)):
    if credentials.credentials != config.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials

# Background task for sending alerts
def send_telegram_alert(message: str):
    notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    notifier.send_message(message)

# API Routes
@app.get("/")
async def root():
    return {"message": "Solana Meme Coin Trader API", "status": "active"}

@app.get("/health")
async def health_check():
    try:
        sol_balance = trader.trader.get_sol_balance()
        usdc_balance = trader.trader.get_usdc_balance()
        return {
            "status": "healthy",
            "wallet_connected": True,
            "sol_balance": sol_balance,
            "usdc_balance": usdc_balance,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check failed: {e}")

@app.post("/trade/buy", response_model=TradeResponse)
async def buy_coin(
    trade_request: TradeRequest,
    background_tasks: BackgroundTasks,
    api_key: HTTPBearer = Depends(verify_api_key)
):
    """Buy a meme coin"""
    try:
        success, result, coin_symbol = trader.buy_with_alert(
            trade_request.coin_symbol,
            trade_request.coin_mint,
            trade_request.amount_usdc,
            trade_request.slippage
        )
        # print("Pydentic hagibo KELA!")
        return TradeResponse(
            success=success,
            message=f"Buy order for {trade_request.amount_usdc} USDC of {trade_request.coin_symbol}",
            transaction_hash=str(result) if success else None,
            timestamp=datetime.now().isoformat(),
            quantity = str(result['quantity'])
        )
    except Exception as e:
        print(str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trade/sell", response_model=TradeResponse)
async def sell_coin(
    sell_request: SellRequest,
    background_tasks: BackgroundTasks,
    api_key: HTTPBearer = Depends(verify_api_key)
):
    """Sell a meme coin"""
    try:
        success, result, amount = trader.sell_with_alert(
            sell_request.coin_symbol,
            sell_request.coin_mint,
            sell_request.percentage
        )
        
        return TradeResponse(
            success=success,
            message=f"Sell order for {sell_request.percentage}% of {sell_request.coin_symbol}",
            transaction_hash=result if success else None,
            timestamp=datetime.now().isoformat(),
            quantity=str(amount)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/portfolio", response_model=PortfolioResponse)
async def get_portfolio(api_key: HTTPBearer = Depends(verify_api_key)):
    """Get current portfolio with all token balances"""
    try:
        sol_balance, usdc_balance, token_balances = trader.get_portfolio()
        return PortfolioResponse(
            sol_balance=sol_balance,
            usdc_balance=usdc_balance,
            token_balances=token_balances
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@app.get("/coins", response_model=List[CoinInfo])
async def get_supported_coins():
    """Get list of supported meme coins"""
    # This would need to be implemented based on your coin list
    # For now, returning empty list
    return []

@app.post("/alert")
async def send_custom_alert(
    alert_request: AlertRequest,
    background_tasks: BackgroundTasks,
    api_key: HTTPBearer = Depends(verify_api_key)
):
    """Send custom alert to Telegram"""
    try:
        background_tasks.add_task(send_telegram_alert, alert_request.message)
        return {"success": True, "message": "Alert sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/balance/usdc")
async def get_usdc_balance(api_key: HTTPBearer = Depends(verify_api_key)):
    """Get USDC balance only"""
    try:
        balance = trader.trader.get_usdc_balance()
        return {"usdc_balance": balance}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/balance/sol")
async def get_sol_balance(api_key: HTTPBearer = Depends(verify_api_key)):
    """Get SOL balance only"""
    try:
        balance = trader.trader.get_sol_balance()
        return {"sol_balance": balance}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/balance/token")
async def get_token_balance(
    balance_request: BalanceRequest,
    api_key: HTTPBearer = Depends(verify_api_key)
):
    """Get specific token balance by mint address"""
    try:
        balance = trader.get_token_balance(balance_request.coin_mint)
        return {
            "coin_mint": balance_request.coin_mint,
            "balance": balance,
            "success": True
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)