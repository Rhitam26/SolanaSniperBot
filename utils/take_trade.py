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
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address
from solders.pubkey import Pubkey
from solders.message import to_bytes_versioned
from dotenv import load_dotenv

load_dotenv()
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
        self.jupiter_api_key = os.getenv('JUPITER_API_KEY')
        
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
    quantity: str

class PortfolioResponse(BaseModel):
    sol_balance: float
    usdc_balance: float
    token_balances: Dict[str, float]
    total_estimated_value: Optional[float] = None

class AlertRequest(BaseModel):
    message: str
    priority: str = "info"

# Updated SolanaTrader class with Jupiter's new API
class SolanaTrader:
    def __init__(self, wallet, rpc_url="https://api.mainnet-beta.solana.com"):
        self.wallet = wallet
        self.sender_pubkey = str(self.wallet.pubkey())
        self.client = Client(rpc_url)
        self.jupiter_base_url = "https://api.jup.ag"
        
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
        except Exception as e:
            print(f"Error getting token balance: {e}")
            return 0
    
    def get_sol_balance(self):
        """Get SOL balance"""
        balance_response = self.client.get_balance(self.wallet.pubkey())
        return balance_response.value / 10**9
    
    def get_usdc_balance(self):
        """Get USDC balance on Mainnet"""
        try:
            usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            balance = self.get_token_balance(usdc_mint)
            print(f"USDC balance check - Mint: {usdc_mint}, Balance: {balance}")
            return balance
        except Exception as e:
            print(f"Error in get_usdc_balance: {e}")
            return 0

    def create_swap_transaction(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Create a swap transaction using Jupiter's new API"""
        try:
            # Prepare headers with API key if available
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if config.jupiter_api_key:
                headers["x-api-key"] = config.jupiter_api_key
            
            # Step 1: Get quote from Jupiter's new API
            quote_url = f"{self.jupiter_base_url}/swap/v1/quote"
            params = {
                "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC on Mainnet
                "outputMint": token_mint,
                "amount": int(1 * 10**6),  # Convert USDC amount to lamports (6 decimals)
                "slippageBps": int(slippage * 100),  # Convert percentage to basis points
                "swapMode": "ExactIn",
                "userPublicKey": self.sender_pubkey
            }
            
            response = requests.get(quote_url, params=params, headers=headers)
            response.raise_for_status()
            quote_data = response.json()
            
            print(f"Quote data: {quote_data}")
            if "routePlan" not in quote_data:
                raise HTTPException(status_code=500, detail="No route found for the swap")
            
            # Step 2: Get swap transaction
            swap_url = f"{self.jupiter_base_url}/swap/v1/swap"
            swap_payload = {
                "userPublicKey": self.sender_pubkey,
                "quoteResponse": quote_data,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "useSharedAccounts": True,
            }
            
            swap_response = requests.post(swap_url, json=swap_payload, headers=headers)
            swap_response.raise_for_status()
            swap_data = swap_response.json()
            
            # Get transaction and sign it
            transaction_base64 = swap_data['swapTransaction']
            transaction_bytes = base64.b64decode(transaction_base64)
            transaction = VersionedTransaction.from_bytes(transaction_bytes)

            signature = self.wallet.sign_message(to_bytes_versioned(transaction.message))
            signed_transaction = VersionedTransaction.populate(transaction.message, [signature])
            
            # Send the transaction
            print("Sending transaction...")
            txn_signature = self.client.send_raw_transaction(bytes(signed_transaction))
            
            print(f"Transaction sent with signature: {txn_signature}")
            print(f"Expected output: {quote_data.get('outAmount')} tokens")

            return {
                'success': True,
                'signature': str(txn_signature),
                'expected_output': quote_data.get('outAmount'),
                'input_amount': quote_data.get('inAmount')
            }
        
        except requests.exceptions.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Jupiter API error: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error creating swap transaction: {str(e)}")
    
    def buy_meme_coin(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Buy meme coin with USDC using Jupiter's new API"""
        try:
            result = self.create_swap_transaction(token_mint, amount_usdc, slippage)
            return True, result
        except Exception as e:
            return False, str(e)
    
    def sell_meme_coin(self, token_mint: str, percentage: float = 100.0):
        """Sell meme coin for USDC using Jupiter's new API"""
        try:
            # Get token balance
            balance = self.get_token_balance(token_mint)
            print(f"Token balance to sell: {balance}")
            
            if balance <= 0:
                return False, "No balance to sell"
            
            # Get token decimals
            token_mint_pubkey = Pubkey.from_string(token_mint)
            response = self.client.get_token_supply(token_mint_pubkey)
            decimals = response.value.decimals
            print(f"Token decimals: {decimals}")
            
            # Calculate amount to sell based on percentage
            amount_to_sell = balance * (percentage / 100.0)
            amount_in_lamports = int(amount_to_sell * (10**decimals))
            print(f"Amount to sell in lamports: {amount_in_lamports}")
            
            # Prepare headers with API key if available
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if config.jupiter_api_key:
                headers["x-api-key"] = config.jupiter_api_key
            
            # Step 1: Get quote for selling
            quote_url = f"{self.jupiter_base_url}/swap/v1/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                "amount": amount_in_lamports,
                "slippageBps": 100,  # 1% slippage
                "swapMode": "ExactIn",
                "userPublicKey": self.sender_pubkey
            }
            
            response = requests.get(quote_url, params=params, headers=headers)
            response.raise_for_status()
            quote_data = response.json()
            
            print(f"Sell quote data: {quote_data}")
            if "routePlan" not in quote_data:
                raise HTTPException(status_code=500, detail="No route found for the swap")
            
            # Step 2: Get swap transaction
            swap_url = f"{self.jupiter_base_url}/swap/v1/swap"
            swap_payload = {
                "userPublicKey": self.sender_pubkey,
                "quoteResponse": quote_data,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "useSharedAccounts": True,
            }
            
            swap_response = requests.post(swap_url, json=swap_payload, headers=headers)
            swap_response.raise_for_status()
            swap_data = swap_response.json()
            
            # Get transaction and sign it
            transaction_base64 = swap_data['swapTransaction']
            transaction_bytes = base64.b64decode(transaction_base64)
            transaction = VersionedTransaction.from_bytes(transaction_bytes)
            
            signature = self.wallet.sign_message(to_bytes_versioned(transaction.message))
            signed_transaction = VersionedTransaction.populate(transaction.message, [signature])
            
            # Send the transaction
            print("Sending sell transaction...")
            txn_signature = self.client.send_raw_transaction(bytes(signed_transaction))
            
            print(f"Sell transaction sent with signature: {txn_signature}")
            print(f"Expected output: {quote_data.get('outAmount')} USDC")
            
            return True, {
                'success': True,
                'signature': str(txn_signature),
                'expected_output': quote_data.get('outAmount'),
                'amount_sold': amount_to_sell
            }
            
        except requests.exceptions.RequestException as e:
            return False, f"Jupiter API error: {str(e)}"
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
  
    def buy_with_alert(self, coin_symbol: str, coin_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Buy meme coin and send notification"""
        try:
            # Validate mint address format
            Pubkey.from_string(coin_mint)
            
            success, result = self.trader.buy_meme_coin(coin_mint, amount_usdc, slippage)
            print(f"Buy result: success={success}, result={result}")

            if success and isinstance(result, dict):
                # Get token decimals for quantity calculation
                token_mint_pubkey = Pubkey.from_string(coin_mint)
                response = self.client.get_token_supply(token_mint_pubkey)
                decimals = response.value.decimals
                
                # Calculate quantity received
                output_amount = int(result.get('expected_output', 0))
                quantity_received = output_amount / (10 ** decimals)
                result['quantity'] = str(quantity_received)
                
                print(f"Quantity received: {quantity_received} tokens")
                
                # Send Telegram alert
                self.notifier.send_trade_alert(
                    f"BUY {coin_symbol}",
                    coin_symbol,
                    f"{amount_usdc} USDC",
                    result.get('signature')
                )
                return True, result, coin_symbol
            else:
                error_msg = f"❌ Buy failed for {coin_symbol}: {result}"
                self.notifier.send_message(error_msg)
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
            print(f"Selling {coin_symbol} ({coin_mint}) at {percentage}%")
            
            success, result = self.trader.sell_meme_coin(coin_mint, percentage)
            
            if success and isinstance(result, dict):
                # Calculate USDC received
                usdc_amount = int(result.get('expected_output', 0)) / (10 ** 6)
                amount_str = f"{usdc_amount:.2f} USDC"
                
                # Send Telegram alert
                self.notifier.send_trade_alert(
                    f"SELL {coin_symbol}",
                    coin_symbol,
                    f"{percentage}% → {amount_str}",
                    result.get('signature')
                )
                return True, str(result), coin_symbol, str(usdc_amount)
            else:
                error_msg = f"❌ Sell failed for {coin_symbol}: {result}"
                self.notifier.send_message(error_msg)
                return False, result, coin_symbol, "0"
                
        except Exception as e:
            error_msg = f"❌ Sell error for {coin_symbol}: {str(e)}"
            self.notifier.send_message(error_msg)
            return False, str(e), coin_symbol, "0"
    
    def get_portfolio(self):
        """Get complete portfolio including SOL, USDC, and all tokens"""
        sol_balance = self.trader.get_sol_balance()
        usdc_balance = self.trader.get_usdc_balance()
        
        print(f"SOL Balance: {sol_balance}")
        print(f"USDC Balance: {usdc_balance}")
        
        # Note: You would need to track token holdings separately
        token_balances = {}
        
        return sol_balance, usdc_balance, token_balances
    
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
            "wallet_connected": config.wallet is not None,
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
    """Buy a meme coin using Jupiter's new API"""
    try:
        success, result, coin_symbol = trader.buy_with_alert(
            trade_request.coin_symbol,
            trade_request.coin_mint,
            trade_request.amount_usdc,
            trade_request.slippage
        )
        
        quantity = result.get('quantity', '0') if isinstance(result, dict) else '0'
        
        return TradeResponse(
            success=success,
            message=f"Buy order for {trade_request.amount_usdc} USDC of {trade_request.coin_symbol}",
            transaction_hash=result.get('signature') if success and isinstance(result, dict) else None,
            timestamp=datetime.now().isoformat(),
            quantity=quantity
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
    """Sell a meme coin using Jupiter's new API"""
    try:
        success, result, coin_symbol, amount = trader.sell_with_alert(
            sell_request.coin_symbol,
            sell_request.coin_mint,
            sell_request.percentage
        )
        
        # Extract signature from result
        transaction_hash = None
        if success and isinstance(result, str):
            try:
                # Try to parse the result to get signature
                import json
                result_dict = json.loads(result)
                transaction_hash = result_dict.get('signature')
            except:
                pass
        
        return TradeResponse(
            success=success,
            message=f"Sell order for {sell_request.percentage}% of {sell_request.coin_symbol}",
            transaction_hash=transaction_hash,
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
