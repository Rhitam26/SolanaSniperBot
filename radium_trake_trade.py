import os
import time
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.transaction import Transaction
from solders.system_program import TransferParams, transfer
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
import base58

# Configuration
# get enviornment variable for private key


RPC_ENDPOINT = "https://api.testnet.solana.com"  # Use your own RPC for better performance
RAYDIUM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
SOL_MINT = "So11111111111111111111111111111111111111112"
STOP_LOSS_PERCENTAGE = 15  # 15% stop loss

class RaydiumTrader:
    def __init__(self, private_key: str):
        """Initialize trader with Phantom wallet private key"""
        self.client = Client(RPC_ENDPOINT)
        self.keypair = Keypair.from_base58_string(private_key)
        self.wallet_address = self.keypair.pubkey()
        print(f"Wallet Address: {self.wallet_address}")
    
    def check_sol_balance(self) -> float:
        """Check SOL balance in wallet"""
        try:
            response = self.client.get_balance(self.wallet_address)
            balance_lamports = response.value
            balance_sol = balance_lamports / 1e9
            print(f"SOL Balance: {balance_sol:.4f} SOL")
            return balance_sol
        except Exception as e:
            print(f"Error checking balance: {e}")
            return 0.0
    
    def check_token_balance(self, token_mint: str) -> float:
        """Check specific token balance"""
        try:
            # Get token accounts for the wallet
            response = self.client.get_token_accounts_by_owner(
                self.wallet_address,
                {"mint": Pubkey.from_string(token_mint)}
            )
            
            if response.value:
                # Parse token amount from first account
                account_data = response.value[0].account.data
                # This is simplified - you'd need to properly decode the token account data
                print(f"Token accounts found for mint: {token_mint}")
                return 0.0  # Placeholder - implement proper decoding
            else:
                print(f"No token account found for mint: {token_mint}")
                return 0.0
        except Exception as e:
            print(f"Error checking token balance: {e}")
            return 0.0
    
    def get_token_price(self, token_mint: str) -> float:
        """Get current token price (placeholder - integrate with price oracle)"""
        # In production, integrate with Jupiter, Pyth, or other price oracles
        print("Note: Implement price oracle integration for real-time prices")
        return 0.0
    
    def execute_swap(self, input_mint: str, output_mint: str, amount: float, slippage: float = 1.0):
        """
        Execute swap on Raydium
        
        Args:
            input_mint: Token to swap from
            output_mint: Token to swap to
            amount: Amount to swap (in token units)
            slippage: Slippage tolerance in percentage
        """
        try:
            print(f"\n=== Executing Swap ===")
            print(f"From: {input_mint}")
            print(f"To: {output_mint}")
            print(f"Amount: {amount}")
            print(f"Slippage: {slippage}%")
            
            # Check balance before trade
            if input_mint == SOL_MINT:
                balance = self.check_sol_balance()
                if balance < amount:
                    print(f"Insufficient balance! Required: {amount} SOL, Available: {balance} SOL")
                    return None
            
            return None
            
        except Exception as e:
            print(f"Error executing swap: {e}")
            return None
    
    def monitor_stop_loss(self, token_mint: str, entry_price: float, position_size: float):
        """
        Monitor position and execute stop loss if triggered
        
        Args:
            token_mint: Token being monitored
            entry_price: Price at which position was entered
            position_size: Size of position in tokens
        """
        stop_loss_price = entry_price * (1 - STOP_LOSS_PERCENTAGE / 100)
        print(f"\n=== Monitoring Stop Loss ===")
        print(f"Entry Price: ${entry_price:.6f}")
        print(f"Stop Loss Price: ${stop_loss_price:.6f} (-{STOP_LOSS_PERCENTAGE}%)")
        print(f"Position Size: {position_size} tokens")
        
        try:
            while True:
                current_price = self.get_token_price(token_mint)
                
                if current_price > 0:
                    pnl_percentage = ((current_price - entry_price) / entry_price) * 100
                    print(f"\nCurrent Price: ${current_price:.6f} | P&L: {pnl_percentage:+.2f}%")
                    
                    if current_price <= stop_loss_price:
                        print(f"\n🚨 STOP LOSS TRIGGERED! 🚨")
                        print(f"Selling {position_size} tokens at ${current_price:.6f}")
                        
                        # Execute sell order
                        self.execute_swap(
                            input_mint=token_mint,
                            output_mint=SOL_MINT,
                            amount=position_size,
                            slippage=2.0  # Higher slippage for emergency exit
                        )
                        break
                
                # Check every 10 seconds
                time.sleep(10)
                
        except KeyboardInterrupt:
            print("\n\nStop loss monitoring stopped by user")
        except Exception as e:
            print(f"Error in stop loss monitoring: {e}")

    def trade_with_stop_loss(self, token_mint: str, sol_amount: float):
        """
        Execute a trade and monitor with stop loss
        
        Args:
            token_mint: Token contract address to buy
            sol_amount: Amount of SOL to spend
        """
        print(f"\n{'='*50}")
        print(f"TRADING WITH STOP LOSS")
        print(f"{'='*50}")
        
        # Check balance
        balance = self.check_sol_balance()
        if balance < sol_amount:
            print(f"❌ Insufficient SOL balance!")
            return
        
        # Get current price before buying
        entry_price = self.get_token_price(token_mint)
        
        # Execute buy
        print(f"\n📈 Buying {sol_amount} SOL worth of token...")
        tx_sig = self.execute_swap(
            input_mint=SOL_MINT,
            output_mint=token_mint,
            amount=sol_amount,
            slippage=1.0
        )
        
        if tx_sig:
            print(f"✅ Buy executed! Transaction: {tx_sig}")
            
            # Calculate position size (simplified)
            position_size = sol_amount / entry_price if entry_price > 0 else 0
            
            # Start monitoring stop loss
            self.monitor_stop_loss(token_mint, entry_price, position_size)
        else:
            print("❌ Trade execution failed!")


# Example usage
if __name__ == "__main__":
    # ⚠️  SECURITY WARNING: Never hardcode private keys!
    # Use environment variables or secure key management
    PRIVATE_KEY = "3MgGGueBjCUeL6QQLoYnQS5jFi5jzBQaDzayttSy7e7Yw2Zh9Z1SzC6H7JH3tiyPLb5tWMMypDe4h9JoZnDgStzH"
    print( f"Private Key: {PRIVATE_KEY}" )
    
    if not PRIVATE_KEY:
        print("❌ Error: PHANTOM_PRIVATE_KEY environment variable not set!")
        print("\nTo use this script:")
        print("1. Export your Phantom wallet private key")
        print("2. Set environment variable: export PHANTOM_PRIVATE_KEY='your_private_key'")
        print("3. Run the script")
        exit(1)
    
    # Initialize trader
    trader = RaydiumTrader(PRIVATE_KEY)
    
    # Example: Trade 0.1 SOL for a token
    # TOKEN_MINT = "YOUR_TOKEN_MINT_ADDRESS_HERE"  # Replace with actual token mint
    # SOL_AMOUNT = 0.1  # Amount of SOL to spend
    
    # Execute trade with stop loss monitoring
    # trader.trade_with_stop_loss(TOKEN_MINT, SOL_AMOUNT)

    # check balance in the wallet
    trader.check_sol_balance()
