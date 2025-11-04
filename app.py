"""
Telegram Channel Message Listener
Complete solution for monitoring messages from a Telegram channel
"""

from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel
import asyncio
import json
from datetime import datetime
import os
import re
from mint_address import get_token_from_dexscreener
from fastapi import FastAPI

# ============================================
# CONFIGURATION
# ============================================

# Get your API credentials from https://my.telegram.org/apps
API_ID = '23265688'  # Replace with your API ID (integer)
API_HASH = '796729041139bc65d33b024ce04f6b5f'  # Replace with your API hash (string)
PHONE = '+919964063864'  # Replace with your phone number (e.g., '+1234567890')

# Channel to monitor (can be username or channel ID)
CHANNEL_USERNAME = '@SOLTRENDING'  # Replace with channel username or ID

# Optional: Save messages to file
SAVE_TO_FILE = True
OUTPUT_FILE = 'telegram_messages.json'

BINANCE_SERVER_URL = "http://localhost:8001/add_symbol"

app = FastAPI()

# ============================================
# SETUP
# ============================================
# TelegramChannelListener class inherits from SolanaPriceTracker to utilize its methods for addition of coins.

class TelegramChannelListener():
    def __init__(self, api_id, api_hash, phone):
        self.client = TelegramClient('session_name', api_id, api_hash)
        self.phone = phone
        self.messages = []
    
    async def start(self):
        """Initialize and start the client"""
        await self.client.start(phone=self.phone)
        print("✓ Client started successfully")
        print(f"✓ Logged in as: {await self.client.get_me()}")
    
    async def get_channel_entity(self, channel_identifier):
        """Get the channel entity"""
        try:
            entity = await self.client.get_entity(channel_identifier)
            print(f"✓ Connected to channel: {entity.title}")
            return entity
        except Exception as e:
            print(f"✗ Error getting channel: {e}")
            print("Make sure you're a member of the channel!")
            return None
    
    def format_message(self, event):
        """Format message data"""
        msg = event.message
        return {
            'id': msg.id,
            'date': msg.date.isoformat(),
            'text': msg.text or '',
            'sender_id': msg.sender_id,
            'views': msg.views,
            'forwards': msg.forwards,
            'has_media': msg.media is not None,
            'media_type': type(msg.media).__name__ if msg.media else None,
            'reply_to': msg.reply_to_msg_id
        }
    
    async def save_message(self, msg_data):
        """Save message to file"""
        if SAVE_TO_FILE:
            self.messages.append(msg_data)
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.messages, f, indent=2, ensure_ascii=False)
    
    async def listen_to_channel(self, channel_identifier):
        """Start listening to channel messages"""
        entity = await self.get_channel_entity(channel_identifier)
        if not entity:
            return
        
        print(f"\n🎧 Listening for new messages...")
        print("Press Ctrl+C to stop\n")
        
        @self.client.on(events.NewMessage(chats=entity))
        async def handler(event):
            msg_data = self.format_message(event)
            
            # Print message info
            print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print(f"📩 New Message (ID: {msg_data['id']})")
            print(f"🕒 Time: {msg_data['date']}")
            print(f"📝 Text: {msg_data['text'][:100]}{'...' if len(msg_data['text']) > 100 else ''}")
            match = re.search(r'/\s*(.*?)\*', msg_data['text'])
            if match:
                print(f"🔗 Extracted COIN SYMBOL: {match.group(1)}")
                token_symbol = match.group(1)
                # address = get_token_from_dexscreener(token_symbol)
                # if address:
                #     print(f"✅ Token Address: {address}")
         

            if msg_data['has_media']:
                print(f"📎 Media: {msg_data['media_type']}")
            print(f"👁️ Views: {msg_data['views']}")
            print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
            
            # Save to file
            await self.save_message(msg_data)
        
        # Keep the client running
        await self.client.run_until_disconnected()
    
    async def get_recent_messages(self, channel_identifier, limit=10):
        """Get recent messages from the channel"""
        entity = await self.get_channel_entity(channel_identifier)
        if not entity:
            return
        
        print(f"\n📥 Fetching last {limit} messages...\n")
        
        async for msg in self.client.iter_messages(entity, limit=limit):
            msg_data = {
                'id': msg.id,
                'date': msg.date.isoformat(),
                'text': msg.text or '',
                'views': msg.views,
                'has_media': msg.media is not None
            }
            print(f"ID {msg_data['id']}: {msg_data['text'][:80]}...")
            await self.save_message(msg_data)

# ============================================
# MAIN EXECUTION
# ============================================

async def main():
    """Main function"""
    print("=" * 50)
    print("Telegram Channel Message Listener")
    print("=" * 50 + "\n")
    
    # Create listener instance
    listener = TelegramChannelListener(API_ID, API_HASH, PHONE)

    try:
        # Start the client
        await listener.start()
        
        # Optional: Get recent messages first
        print("\n" + "=" * 50)
        print("Do you want to fetch recent messages first? (y/n)")
        print("=" * 50)
        # await listener.get_recent_messages(CHANNEL_USERNAME, limit=10)
        
        # Start listening for new messages
        await listener.listen_to_channel(CHANNEL_USERNAME)
        
    except KeyboardInterrupt:
        print("\n\n✓ Listener stopped by user")
    except Exception as e:
        print(f"\n✗ Error: {e}")
    finally:
        await listener.client.disconnect()
        print("✓ Client disconnected")

if __name__ == '__main__':
    asyncio.run(main())