import os
import logging
from telegram import Bot
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def test_bot():
    """Test if the bot is working"""
    try:
        # Get bot token
        token = os.getenv('BOT_TOKEN')
        if not token:
            logger.error("No bot token found!")
            return

        # Initialize bot
        bot = Bot(token=token)
        
        # Get bot info
        bot_info = await bot.get_me()
        logger.info(f"Bot is working! Bot username: @{bot_info.username}")
        
        # Test admin group notification
        admin_group_id = os.getenv('ADMIN_GROUP_ID')
        if admin_group_id:
            await bot.send_message(
                chat_id=admin_group_id,
                text="🤖 Bot is now running on Railway!"
            )
            logger.info("Admin notification sent successfully!")
        
    except Exception as e:
        logger.error(f"Error testing bot: {e}")

if __name__ == '__main__':
    import asyncio
    asyncio.run(test_bot()) 