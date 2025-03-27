import os
import logging
from datetime import datetime
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import BadRequest
import requests
from blockcypher import get_transaction_details, get_address_details
from nowpayments import NOWPayments
import json

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize NOWPayments API
nowpayments = NOWPayments(api_key=os.getenv('NOWPAYMENTS_API_KEY'))

# Store active transactions
active_transactions = {}

# Load blocked users
BLOCKED_USERS_FILE = 'blocked_users.json'
blocked_users = set()

def load_blocked_users():
    try:
        with open(BLOCKED_USERS_FILE, 'r') as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()

def save_blocked_users():
    with open(BLOCKED_USERS_FILE, 'w') as f:
        json.dump(list(blocked_users), f)

# Load blocked users on startup
blocked_users = load_blocked_users()

# Constants
OWNER_CHANNEL = "https://t.me/your_owner_channel"
ADMIN_CHANNEL = "https://t.me/your_admin_channel"
VOUCH_CHANNEL = "https://t.me/your_vouch_channel"
ESCROW_FEE_PERCENTAGE = float(os.getenv('ESCROW_FEE_PERCENTAGE', 5))
BOT_OWNER_ID = int(os.getenv('BOT_OWNER_ID', 0))
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]
ADMIN_GROUP_ID = int(os.getenv('ADMIN_GROUP_ID', 0))

def is_admin(user_id: int) -> bool:
    """Check if user is an admin"""
    return user_id in ADMIN_IDS

def is_owner(user_id: int) -> bool:
    """Check if user is the bot owner"""
    return user_id == BOT_OWNER_ID

def is_blocked(user_id: int) -> bool:
    """Check if user is blocked"""
    return user_id in blocked_users

async def check_bot_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if bot has required permissions in the group"""
    try:
        bot_member = await update.effective_chat.get_member(context.bot.id)
        return bot_member.can_restrict_members and bot_member.can_delete_messages
    except BadRequest:
        return False

def detect_crypto_type(address: str) -> str:
    """Detect if address is BTC or LTC"""
    if address.startswith('1') or address.startswith('3') or address.startswith('bc1'):
        return 'btc'
    elif address.startswith('L') or address.startswith('M') or address.startswith('ltc1'):
        return 'ltc'
    else:
        raise ValueError("Invalid cryptocurrency address")

# Add new transaction monitoring variables
MONITORING_INTERVAL = 60  # Check every 60 seconds
monitored_transactions = {}

async def monitor_transaction(chat_id: int, tx_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Monitor a transaction and send updates"""
    last_status = None
    while True:
        try:
            # Try BTC first
            try:
                tx_info = get_transaction_details(tx_id, coin_symbol='btc')
                coin_type = 'BTC'
            except:
                # If not BTC, try LTC
                tx_info = get_transaction_details(tx_id, coin_symbol='ltc')
                coin_type = 'LTC'
            
            if tx_info:
                current_status = "Confirmed" if tx_info.get('confirmations', 0) > 0 else "Pending"
                amount = tx_info.get('total', 0) / 100000000  # Convert satoshis to BTC/LTC
                confirmations = tx_info.get('confirmations', 0)
                
                # Only send update if status changed
                if current_status != last_status:
                    message = f"""
🔄 Transaction Update
Status: {current_status}
Amount: {amount} {coin_type}
Confirmations: {confirmations}
                    """
                    await context.bot.send_message(chat_id=chat_id, text=message)
                    
                    # If confirmed, update transaction status
                    if current_status == "Confirmed" and chat_id in active_transactions:
                        active_transactions[chat_id]['payment_status'] = 'confirmed'
                        message = "✅ Payment confirmed! You can now use /release to release the funds."
                        await context.bot.send_message(chat_id=chat_id, text=message)
                        # Stop monitoring after confirmation
                        break
                
                last_status = current_status
            
            # Wait before next check
            await asyncio.sleep(MONITORING_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error monitoring transaction: {e}")
            await asyncio.sleep(MONITORING_INTERVAL)

async def start_monitoring(chat_id: int, tx_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Start monitoring a transaction"""
    if chat_id not in monitored_transactions:
        monitored_transactions[chat_id] = set()
    
    if tx_id not in monitored_transactions[chat_id]:
        monitored_transactions[chat_id].add(tx_id)
        asyncio.create_task(monitor_transaction(chat_id, tx_id, context))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command"""
    if is_blocked(update.effective_user.id):
        await update.message.reply_text("You are blocked from using this bot.")
        return

    if update.effective_chat.type == 'private':
        keyboard = [
            [InlineKeyboardButton("Help", callback_data='help')],
            [InlineKeyboardButton("Links", callback_data='links')],
            [InlineKeyboardButton("Vouches", callback_data='vouches')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Welcome to the Escrow Bot! Please use this bot in a group chat for transactions.\n"
            "Use /help to see available commands.",
            reply_markup=reply_markup
        )
    else:
        # Check bot permissions
        if not await check_bot_permissions(update, context):
            await update.message.reply_text(
                "⚠️ This bot requires admin privileges to function properly!\n"
                "Please make the bot an admin with the following permissions:\n"
                "- Delete messages\n"
                "- Restrict members"
            )
            return
        await update.message.reply_text("Bot is ready to handle escrow transactions!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command"""
    if update.effective_chat.type == 'private':
        help_text = """
Available Commands:
/start - Start the bot
/help - Show this help message
/links - View owner and admin links
/vouches - View vouch channel

Group Commands:
/buyer <address> - Set buyer role with crypto address
/seller <address> - Set seller role with crypto address
/transaction <id> - Check transaction status
/release - Release funds to the other party
        """
        await update.message.reply_text(help_text)
    else:
        await update.message.reply_text("This command is only available in private chat!")

async def links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /links command"""
    if update.effective_chat.type == 'private':
        links_text = f"""
Owner Channel: {OWNER_CHANNEL}
Admin Channel: {ADMIN_CHANNEL}
        """
        await update.message.reply_text(links_text)
    else:
        await update.message.reply_text("This command is only available in private chat!")

async def vouches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /vouches command"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text(f"View our vouch channel: {VOUCH_CHANNEL}")
    else:
        await update.message.reply_text("This command is only available in private chat!")

async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a payment request using NOWPayments"""
    chat_id = update.effective_chat.id
    if chat_id not in active_transactions:
        await update.message.reply_text("No active transaction found. Please set up buyer and seller first!")
        return

    transaction = active_transactions[chat_id]
    if 'amount' not in transaction:
        await update.message.reply_text("Please specify the amount first!")
        return

    try:
        # Create payment request
        payment = nowpayments.create_payment(
            price_amount=transaction['amount'],
            price_currency='usd',
            order_id=f"escrow_{chat_id}_{datetime.now().timestamp()}",
            order_description=f"Escrow transaction in chat {chat_id}"
        )

        # Store payment info
        transaction['payment_id'] = payment['payment_id']
        transaction['payment_address'] = payment['pay_address']
        transaction['payment_status'] = 'pending'

        message = f"""
Payment Request Created:
Amount: {transaction['amount']} USD
Address: {payment['pay_address']}
Payment ID: {payment['payment_id']}

Please send the payment to the address above. The bot will monitor the transaction and notify when payment is received.
        """
        await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"Error creating payment: {e}")
        await update.message.reply_text("Error creating payment request. Please try again later.")

async def check_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /transaction command"""
    if update.effective_chat.type == 'group':
        if not context.args:
            await update.message.reply_text("Please provide a transaction ID!")
            return
        
        tx_id = context.args[0]
        chat_id = update.effective_chat.id
        
        try:
            # Start monitoring the transaction
            await start_monitoring(chat_id, tx_id, context)
            
            # Initial check
            try:
                tx_info = get_transaction_details(tx_id, coin_symbol='btc')
                coin_type = 'BTC'
            except:
                tx_info = get_transaction_details(tx_id, coin_symbol='ltc')
                coin_type = 'LTC'
            
            if tx_info:
                status = "Confirmed" if tx_info.get('confirmations', 0) > 0 else "Pending"
                amount = tx_info.get('total', 0) / 100000000
                from_address = tx_info.get('inputs', [{}])[0].get('addresses', ['Unknown'])[0]
                to_address = tx_info.get('outputs', [{}])[0].get('addresses', ['Unknown'])[0]
                
                message = f"""
Transaction Status: {status}
Amount: {amount} {coin_type}
From: {from_address}
To: {to_address}
Confirmations: {tx_info.get('confirmations', 0)}

I will now monitor this transaction and send updates when the status changes.
                """
                
                await update.message.reply_text(message)
            else:
                await update.message.reply_text("Transaction not found!")
                
        except Exception as e:
            logger.error(f"Error checking transaction: {e}")
            await update.message.reply_text("Error checking transaction status. Please try again later.")
    else:
        await update.message.reply_text("This command only works in group chats!")

async def set_buyer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /buyer command"""
    if is_blocked(update.effective_user.id):
        await update.message.reply_text("You are blocked from using this bot.")
        return

    if update.effective_chat.type == 'group':
        if not await check_bot_permissions(update, context):
            await update.message.reply_text("Bot needs admin privileges to function!")
            return

        if not context.args:
            await update.message.reply_text("Please provide a cryptocurrency address!")
            return
        
        try:
            address = context.args[0]
            coin_type = detect_crypto_type(address)
            
            # Verify address is valid
            address_info = get_address_details(address, coin_symbol=coin_type)
            if not address_info:
                await update.message.reply_text("Invalid cryptocurrency address!")
                return
            
            chat_id = update.effective_chat.id
            if chat_id not in active_transactions:
                active_transactions[chat_id] = {}
            
            active_transactions[chat_id]['buyer'] = {
                'address': address,
                'coin_type': coin_type,
                'timestamp': datetime.now().isoformat()
            }
            
            await update.message.reply_text(f"Buyer role set with {coin_type.upper()} address: {address}")
        except ValueError as e:
            await update.message.reply_text(str(e))
        except Exception as e:
            logger.error(f"Error setting buyer: {e}")
            await update.message.reply_text("Error setting buyer address. Please try again later.")
    else:
        await update.message.reply_text("This command only works in group chats!")

async def set_seller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /seller command"""
    if is_blocked(update.effective_user.id):
        await update.message.reply_text("You are blocked from using this bot.")
        return

    if update.effective_chat.type == 'group':
        if not await check_bot_permissions(update, context):
            await update.message.reply_text("Bot needs admin privileges to function!")
            return

        if not context.args:
            await update.message.reply_text("Please provide a cryptocurrency address!")
            return
        
        try:
            address = context.args[0]
            coin_type = detect_crypto_type(address)
            
            # Verify address is valid
            address_info = get_address_details(address, coin_symbol=coin_type)
            if not address_info:
                await update.message.reply_text("Invalid cryptocurrency address!")
                return
            
            chat_id = update.effective_chat.id
            if chat_id not in active_transactions:
                active_transactions[chat_id] = {}
            
            active_transactions[chat_id]['seller'] = {
                'address': address,
                'coin_type': coin_type,
                'timestamp': datetime.now().isoformat()
            }
            
            await update.message.reply_text(f"Seller role set with {coin_type.upper()} address: {address}")
        except ValueError as e:
            await update.message.reply_text(str(e))
        except Exception as e:
            logger.error(f"Error setting seller: {e}")
            await update.message.reply_text("Error setting seller address. Please try again later.")
    else:
        await update.message.reply_text("This command only works in group chats!")

async def release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /release command"""
    if update.effective_chat.type != 'group':
        await update.message.reply_text("This command only works in group chats!")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in active_transactions:
        await update.message.reply_text("No active transaction found!")
        return

    transaction = active_transactions[chat_id]
    
    # Check if user is either buyer or seller
    if 'buyer' not in transaction or 'seller' not in transaction:
        await update.message.reply_text("Both buyer and seller must be set up first!")
        return

    if transaction['buyer']['user_id'] != user_id and transaction['seller']['user_id'] != user_id:
        await update.message.reply_text("Only the buyer or seller can release funds!")
        return

    if 'payment_status' not in transaction or transaction['payment_status'] != 'confirmed':
        await update.message.reply_text("Payment must be confirmed before release!")
        return

    try:
        # Calculate amounts with escrow fee
        total_amount = float(transaction['amount'])
        escrow_fee = total_amount * (ESCROW_FEE_PERCENTAGE / 100)
        release_amount = total_amount - escrow_fee

        # Determine release direction based on who initiated the release
        if user_id == transaction['buyer']['user_id']:
            release_address = transaction['seller']['address']
            releaser = "Buyer"
        else:
            release_address = transaction['buyer']['address']
            releaser = "Seller"

        # Create release payment
        release_payment = nowpayments.create_payment(
            price_amount=release_amount,
            price_currency='usd',
            order_id=f"release_{chat_id}_{datetime.now().timestamp()}",
            order_description=f"Release payment for escrow transaction {chat_id}",
            pay_address=release_address
        )

        message = f"""
Release Initiated by {releaser}:
Amount: {release_amount} USD
Address: {release_address}
Payment ID: {release_payment['payment_id']}

The funds will be released to the specified address. The escrow fee ({ESCROW_FEE_PERCENTAGE}%) has been deducted.
        """
        await update.message.reply_text(message)

        # Clear the transaction
        del active_transactions[chat_id]

    except Exception as e:
        logger.error(f"Error releasing funds: {e}")
        await update.message.reply_text("Error releasing funds. Please try again later.")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /admin command"""
    if update.effective_chat.type != 'group':
        await update.message.reply_text("This command only works in group chats!")
        return

    chat_id = update.effective_chat.id
    chat_link = await update.effective_chat.get_invite_link()
    
    # Notify admins
    admin_message = f"""
🔔 Admin Help Request
From: {update.effective_chat.title}
Chat ID: {chat_id}
Link: {chat_link}
Requested by: {update.effective_user.mention_html()}
    """
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_message,
            parse_mode='HTML'
        )
        await update.message.reply_text("✅ Admin has been notified and will join shortly!")
    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")
        await update.message.reply_text("❌ Error notifying admin. Please try again later.")

async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /block command (admin only)"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is only available to admins!")
        return

    if not context.args:
        await update.message.reply_text("Please provide a user ID to block!")
        return

    try:
        user_id = int(context.args[0])
        blocked_users.add(user_id)
        save_blocked_users()
        await update.message.reply_text(f"User {user_id} has been blocked from using the bot.")
    except ValueError:
        await update.message.reply_text("Invalid user ID!")

async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /unblock command (admin only)"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is only available to admins!")
        return

    if not context.args:
        await update.message.reply_text("Please provide a user ID to unblock!")
        return

    try:
        user_id = int(context.args[0])
        blocked_users.discard(user_id)
        save_blocked_users()
        await update.message.reply_text(f"User {user_id} has been unblocked.")
    except ValueError:
        await update.message.reply_text("Invalid user ID!")

async def refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /refund command (admin only)"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is only available to admins!")
        return

    if not context.args:
        await update.message.reply_text("Please provide a transaction ID to refund!")
        return

    tx_id = context.args[0]
    chat_id = update.effective_chat.id

    if chat_id not in active_transactions:
        await update.message.reply_text("No active transaction found!")
        return

    transaction = active_transactions[chat_id]
    if 'payment_id' not in transaction:
        await update.message.reply_text("No payment found for this transaction!")
        return

    try:
        # Create refund through NOWPayments
        refund = nowpayments.create_refund(
            payment_id=transaction['payment_id'],
            reason="Admin initiated refund"
        )

        message = f"""
🔔 Admin Refund Initiated
Transaction ID: {tx_id}
Amount: {transaction['amount']} USD
Refund ID: {refund['refund_id']}
        """
        
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=message
        )
        
        await update.message.reply_text("Refund has been initiated. The transaction will be cancelled.")
        del active_transactions[chat_id]

    except Exception as e:
        logger.error(f"Error processing refund: {e}")
        await update.message.reply_text("Error processing refund. Please try again later.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /stats command (admin only)"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is only available to admins!")
        return

    total_transactions = len(active_transactions)
    blocked_count = len(blocked_users)
    
    stats_message = f"""
📊 Bot Statistics
Active Transactions: {total_transactions}
Blocked Users: {blocked_count}
    """
    
    await update.message.reply_text(stats_message)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries from inline buttons"""
    query = update.callback_query
    await query.answer()

    if query.data == 'help':
        await help_command(update, context)
    elif query.data == 'links':
        await links(update, context)
    elif query.data == 'vouches':
        await vouches(update, context)

def main():
    """Start the bot"""
    # Get bot token from environment variable
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("No bot token found! Please set BOT_TOKEN in .env file")
        return

    # Create application
    application = Application.builder().token(token).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("links", links))
    application.add_handler(CommandHandler("vouches", vouches))
    application.add_handler(CommandHandler("buyer", set_buyer))
    application.add_handler(CommandHandler("seller", set_seller))
    application.add_handler(CommandHandler("transaction", check_transaction))
    application.add_handler(CommandHandler("release", release))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("block", block_user))
    application.add_handler(CommandHandler("unblock", unblock_user))
    application.add_handler(CommandHandler("refund", refund))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main() 