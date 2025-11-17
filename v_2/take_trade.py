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
from spl.token.instructions import get_associated_token_address, create_associated_token_account
from solders.pubkey import Pubkey

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
        self.private_key = ""
        self.telegram_bot_token = '1959341927:AAHjMpXGhZWvLN8R-gTozl4NTlmCsyonGw8'
        self.telegram_chat_id = '1730454128'
        self.rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.api_key = ''
        self.jupiter_api_key =  ""
        
        # Initialize wallet
        if self.private_key:
            private_key_bytes = base58.b58decode(self.private_key)
            self.wallet = Keypair.from_bytes(private_key_bytes)
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
        balance_response = self.client.get_balance(self.wallet.pubkey())
        return balance_response.value / 10**9
    
    # def get_usdc_balance(self):
    #     """Get USDC balance"""
    #     # USDC mint address on Devnet
    #     usdc_mint = Pubkey.from_string("4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU")
    #     return self.get_token_balance(usdc_mint)
    
    # def get_usdc_balance(self):
    #     """Get USDC balance with better debugging"""
    #     try:
    #         # USDC mint addresses
    #         print("Hi..")
    #         usdc_mints = [
    #             "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    #         ]
            
    #         for usdc_mint in usdc_mints:
    #             try:
    #                 balance = self.get_token_balance(usdc_mint)
    #                 if balance > 0:
    #                     print(f"Found USDC balance: {balance} using mint: {usdc_mint}")
    #                     return balance
    #             except Exception as e:
    #                 print(f"Error checking USDC mint {usdc_mint}: {e}")
    #                 continue
            
    #         # If no balance found, check all token accounts
    #         print("Checking all token accounts for USDC...")
    #         return 0
    #     except Exception as e:
    #         print(f"Error in get_usdc_balance: {e}")
    #         return 0

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

    
    # def get_all_token_balances(self):
    #     """Get balances of all tokens in the wallet"""
    #     try:
    #         # Get all token accounts
    #         response = self.client.get_token_accounts_by_owner(
    #             self.wallet.pubkey(),
    #             program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    #         )
            
    #         token_balances = {}
            
    #         for account in response.value:
    #             # Get token account info
    #             account_info = self.client.get_token_account_balance(account.pubkey)
    #             if account_info.value:
    #                 mint = str(account.account.data.parsed['info']['mint'])
    #                 balance = float(account_info.value.amount) / 10**account_info.value.decimals
                    
    #                 # Only include tokens with non-zero balance
    #                 if balance > 0:
    #                     token_balances[mint] = balance
            
    #         return token_balances
    #     except Exception as e:
    #         print(f"Error getting all token balances: {e}")
    #         return {}
    
    def create_swap_transaction(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Create a swap transaction using Jupiter API"""
        try:
            # Get quote from Jupiter
            jupiter_url = "https://api.jup.ag/ultra/v6/quote"
            params = {
                "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC on Mainnet
                # "inputMint": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",  # USDC on Devnet
                "outputMint": token_mint,
                "amount": int(amount_usdc * 10**6),  # Assuming USDC has 6 decimals
                "slippageBps": int(slippage * 100)
            }
            headers = {"Accept": "application/json", "Authorization": f"Bearer {config.jupiter_api_key}"}
            
            response = requests.get(jupiter_url, params=params, headers=headers)
            quote_data = response.json()
            print(f"Quote data: {quote_data}")
            # Get swap transaction
            swap_url = "https://api.jup.ag/ultra/v6/swap"
            swap_data = {
                "quoteResponse": quote_data,
                "userPublicKey": str(self.wallet.pubkey()),
                "wrapAndUnwrapSol": True
            }
            
            headers = {"Content-Type": "application/json"}
            swap_response = requests.post(swap_url, json=swap_data, headers=headers)
            swap_transaction_data = swap_response.json()
            print(f"Swap transaction data: {swap_transaction_data}")
            
            return swap_transaction_data["swapTransaction"]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error creating swap transaction: {e}")
    
    def execute_swap(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Execute a swap transaction"""
        try:
            swap_transaction = self.create_swap_transaction(token_mint, amount_usdc, slippage)
            if not swap_transaction:
                return False, "Failed to create swap transaction"
            
            # Deserialize transaction
            transaction_bytes = base64.b64decode(swap_transaction)
            transaction = Transaction.deserialize(transaction_bytes)
            
            # Send transaction
            result = self.client.send_transaction(transaction, self.wallet)
            print(f"Swap executed: {result}")
            return True, result.value
        except Exception as e:
            return False, str(e)
    
    def buy_meme_coin(self, token_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Buy meme coin with USDC"""
        return self.execute_swap(token_mint, amount_usdc, slippage)
    
    def sell_meme_coin(self, token_mint: str, percentage: float = 100.0):
        """Sell meme coin for USDC"""
        try:
            balance = self.get_token_balance(token_mint)
            if balance <= 0:
                return False, "No balance to sell"
            
            # Get quote for selling
            jupiter_url = "https://api.jup.ag/ultra/v6/quote"
            params = {
                "inputMint": token_mint,
                "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC on Mainnet
                # "outputMint": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",  # USDC on Devnet
                "amount": int(balance * 10**6),  # Assuming 6 decimals
                "slippageBps": 1000,  # 10% slippage for meme coins,
                "cluster": "devnet"
            }
            
            response = requests.get(jupiter_url, params=params)
            quote_data = response.json()
            
            # Execute swap
            swap_url = "https://api.jup.ag/ultra/v6/swap"
            swap_data = {
                "quoteResponse": quote_data,
                "userPublicKey": str(self.wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "cluster": "devnet"
            }
            
            headers = {"Content-Type": "application/json"}
            swap_response = requests.post(swap_url, json=swap_data, headers=headers)
            swap_transaction_data = swap_response.json()
            
            transaction_bytes = base64.b64decode(swap_transaction_data["swapTransaction"])
            transaction = Transaction.deserialize(transaction_bytes)
            
            result = self.client.send_transaction(transaction, self.wallet)
            return True, result.value
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
    
    def buy_with_alert(self, coin_symbol: str, coin_mint: str, amount_usdc: float, slippage: float = 10.0):
        """Buy meme coin and send notification"""
        try:
            # Validate mint address format
            Pubkey.from_string(coin_mint)
            
            success, result = self.trader.buy_meme_coin(coin_mint, amount_usdc, slippage)
            print(f"Buy result: success={success}, result={result}")
            
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
            
            success, result = self.trader.sell_meme_coin(coin_mint, percentage)
            
            if success:
                self.notifier.send_trade_alert(
                    f"SELL {coin_symbol}",
                    coin_symbol,
                    f"{percentage}%",
                    result
                )
                return True, result, coin_symbol
            else:
                self.notifier.send_message(f"❌ Sell failed for {coin_symbol}: {result}")
                return False, result, coin_symbol
                
        except Exception as e:
            error_msg = f"❌ Sell error for {coin_symbol}: {str(e)}"
            self.notifier.send_message(error_msg)
            return False, str(e), coin_symbol
    
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

        
        return TradeResponse(
            success=success,
            message=f"Buy order for {trade_request.amount_usdc} USDC of {trade_request.coin_symbol}",
            transaction_hash=result if success else None,
            timestamp=datetime.now().isoformat()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trade/sell", response_model=TradeResponse)
async def sell_coin(
    sell_request: SellRequest,
    background_tasks: BackgroundTasks,
    api_key: HTTPBearer = Depends(verify_api_key)
):
    """Sell a meme coin"""
    try:
        success, result, coin_symbol = trader.sell_with_alert(
            sell_request.coin_symbol,
            sell_request.coin_mint,
            sell_request.percentage
        )
        
        return TradeResponse(
            success=success,
            message=f"Sell order for {sell_request.percentage}% of {sell_request.coin_symbol}",
            transaction_hash=result if success else None,
            timestamp=datetime.now().isoformat()
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
