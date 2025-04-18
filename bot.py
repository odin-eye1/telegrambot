import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters
from telegram.error import BadRequest, NetworkError, TimedOut, RetryAfter
import requests
from blockcypher import get_transaction_details, get_address_details
from nowpayments import NOWPayments
import json
import traceback
import time

# Load environment variables
load_dotenv()

# Configure logging with more detailed format
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s - [%(filename)s:%(lineno)d]',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize NOWPayments API
nowpayments = NOWPayments(api_key=os.getenv('NOWPAYMENTS_API_KEY'))

# Store active transactions
active_transactions = {}

# Load blocked users
BLOCKED_USERS_FILE = 'blocked_users.json'
blocked_users = set()

# Cleanup settings
CLEANUP_INTERVAL = 3600  # Clean up every hour
TRANSACTION_TIMEOUT = 86400  # 24 hours timeout for transactions

def load_blocked_users():
    try:
        with open(BLOCKED_USERS_FILE, 'r') as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except json.JSONDecodeError as e:
        logger.error(f"Error loading blocked users: {e}")
        return set()

def save_blocked_users():
    try:
        with open(BLOCKED_USERS_FILE, 'w') as f:
            json.dump(list(blocked_users), f)
    except Exception as e:
        logger.error(f"Error saving blocked users: {e}")

# Load blocked users on startup
blocked_users = load_blocked_users()

# Constants
OWNER_CHANNEL = "https://t.me/redirectosakura"
ADMIN_CHANNEL = "https://t.me/redirectosakura"  # Using owner channel for now
VOUCH_CHANNEL = "https://t.me/redirectosakura"  # Using owner channel for now
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
    except BadRequest as e:
        logger.error(f"Error checking bot permissions: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking bot permissions: {e}")
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

def cleanup_old_transactions(context):
    """Clean up old transactions"""
    try:
        current_time = datetime.now()
        expired_chats = []

        for chat_id, transaction in active_transactions.items():
            # Check if transaction is too old
            if 'timestamp' in transaction:
                transaction_time = datetime.fromisoformat(transaction['timestamp'])
                if (current_time - transaction_time).total_seconds() > TRANSACTION_TIMEOUT:
                    expired_chats.append(chat_id)
                    logger.info(f"Cleaning up expired transaction in chat {chat_id}")

        # Remove expired transactions
        for chat_id in expired_chats:
            del active_transactions[chat_id]
            try:
                context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Transaction has expired due to inactivity. Please start a new transaction if needed."
                )
            except Exception as e:
                logger.error(f"Error sending cleanup message to chat {chat_id}: {e}")

        logger.info(f"Cleanup completed. Removed {len(expired_chats)} expired transactions.")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

def handle_api_error(e, update, context, operation):
    """Handle API errors and notify appropriate parties"""
    error_message = f"Error during {operation}: {str(e)}"
    logger.error(error_message)
    logger.error(traceback.format_exc())

    # Notify admins
    admin_message = f"""
🚨 API Error Alert
Operation: {operation}
Error: {str(e)}
Chat ID: {update.effective_chat.id if update.effective_chat else 'N/A'}
User: {update.effective_user.id if update.effective_user else 'N/A'}
    """
    
    try:
        context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=admin_message
        )
    except Exception as admin_error:
        logger.error(f"Error sending admin notification: {admin_error}")

    # Notify user if possible
    if update.effective_chat:
        try:
            update.message.reply_text(
                f"Sorry, there was an error during {operation}. "
                "The admin has been notified and will look into it."
            )
        except Exception as user_error:
            logger.error(f"Error sending user notification: {user_error}")

def monitor_transaction(chat_id, tx_id, context):
    """Monitor a transaction and send updates"""
    last_status = None
    retry_count = 0
    max_retries = 3

    while True:
        try:
            # Try BTC first
            try:
                tx_info = get_transaction_details(tx_id, coin_symbol='btc')
                coin_type = 'BTC'
            except Exception as e:
                # If not BTC, try LTC
                try:
                    tx_info = get_transaction_details(tx_id, coin_symbol='ltc')
                    coin_type = 'LTC'
                except Exception as ltc_error:
                    raise Exception(f"Failed to get transaction details: {str(e)} | {str(ltc_error)}")
            
            if tx_info:
                current_status = "Confirmed" if tx_info.get('confirmations', 0) > 0 else "Pending"
                amount = tx_info.get('total', 0) / 100000000
                confirmations = tx_info.get('confirmations', 0)
                
                if current_status != last_status:
                    message = f"""
🔄 Transaction Update
Status: {current_status}
Amount: {amount} {coin_type}
Confirmations: {confirmations}
                    """
                    context.bot.send_message(chat_id=chat_id, text=message)
                    
                    if current_status == "Confirmed" and chat_id in active_transactions:
                        active_transactions[chat_id]['payment_status'] = 'confirmed'
                        message = "✅ Payment confirmed! You can now use /release to release the funds."
                        context.bot.send_message(chat_id=chat_id, text=message)
                        break
                
                last_status = current_status
                retry_count = 0  # Reset retry count on successful operation
            
            time.sleep(MONITORING_INTERVAL)
            
        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                handle_api_error(e, Update(update_id=0), context, "transaction monitoring")
                break
            time.sleep(MONITORING_INTERVAL * retry_count)  # Exponential backoff

def start_monitoring(chat_id, tx_id, context):
    """Start monitoring a transaction"""
    if chat_id not in monitored_transactions:
        monitored_transactions[chat_id] = set()
    
    if tx_id not in monitored_transactions[chat_id]:
        monitored_transactions[chat_id].add(tx_id)
        import threading
        thread = threading.Thread(target=monitor_transaction, args=(chat_id, tx_id, context))
        thread.daemon = True
        thread.start()

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
    """Create a payment using NOWPayments API"""
    try:
        amount = float(context.user_data.get('amount', 0))
        if amount <= 0:
            await update.message.reply_text("❌ Invalid amount. Please try again.")
            return

        # NOWPayments API endpoint
        url = "https://api.nowpayments.io/v1/payment"
        
        # Headers
        headers = {
            "x-api-key": os.getenv('NOWPAYMENTS_API_KEY'),
            "Content-Type": "application/json"
        }
        
        # Request body
        data = {
            "price_amount": amount,
            "price_currency": "usd",
            "order_id": f"order_{int(time.time())}",
            "order_description": "Escrow Transaction",
            "ipn_callback_url": "https://your-domain.com/ipn",  # You'll need to set this up
            "success_url": "https://t.me/redirectosakura",
            "cancel_url": "https://t.me/redirectosakura"
        }
        
        # Make the API request
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()  # Raise an exception for bad status codes
        
        result = response.json()
        
        if 'payment_url' in result:
            keyboard = [
                [InlineKeyboardButton("Pay Now", url=result['payment_url'])]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"✅ Payment created successfully!\n\n"
                f"Amount: ${amount:.2f}\n"
                f"Payment ID: {result.get('payment_id', 'N/A')}\n"
                f"Status: {result.get('payment_status', 'pending')}\n\n"
                f"Click the button below to complete your payment:",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("❌ Failed to create payment. Please try again.")
            
    except requests.exceptions.RequestException as e:
        logging.error(f"NOWPayments API error: {str(e)}")
        await update.message.reply_text("❌ Error creating payment. Please try again later.")
    except Exception as e:
        logging.error(f"Error in create_payment: {str(e)}")
        await update.message.reply_text("❌ An unexpected error occurred. Please try again later.")

async def check_payment_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check payment status using NOWPayments API"""
    try:
        payment_id = context.user_data.get('payment_id')
        if not payment_id:
            await update.message.reply_text("❌ No payment ID found. Please create a payment first.")
            return

        # NOWPayments API endpoint
        url = f"https://api.nowpayments.io/v1/payment/{payment_id}"
        
        # Headers
        headers = {
            "x-api-key": os.getenv('NOWPAYMENTS_API_KEY')
        }
        
        # Make the API request
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        result = response.json()
        
        status = result.get('payment_status', 'unknown')
        amount = result.get('price_amount', 0)
        currency = result.get('price_currency', 'USD')
        
        await update.message.reply_text(
            f"Payment Status:\n\n"
            f"Amount: {amount} {currency}\n"
            f"Status: {status}\n"
            f"Payment ID: {payment_id}"
        )
        
    except requests.exceptions.RequestException as e:
        logging.error(f"NOWPayments API error: {str(e)}")
        await update.message.reply_text("❌ Error checking payment status. Please try again later.")
    except Exception as e:
        logging.error(f"Error in check_payment_status: {str(e)}")
        await update.message.reply_text("❌ An unexpected error occurred. Please try again later.")

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
    try:
        # Get bot token from environment variable
        token = os.getenv('BOT_TOKEN')
        if not token:
            logger.error("No bot token found! Please set BOT_TOKEN in .env file")
            return

        # Create updater
        updater = Updater(token, use_context=True)
        dispatcher = updater.dispatcher

        # Add handlers
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("help", help_command))
        dispatcher.add_handler(CommandHandler("links", links))
        dispatcher.add_handler(CommandHandler("vouches", vouches))
        dispatcher.add_handler(CommandHandler("buyer", set_buyer))
        dispatcher.add_handler(CommandHandler("seller", set_seller))
        dispatcher.add_handler(CommandHandler("transaction", check_transaction))
        dispatcher.add_handler(CommandHandler("release", release))
        dispatcher.add_handler(CommandHandler("admin", admin_command))
        dispatcher.add_handler(CommandHandler("block", block_user))
        dispatcher.add_handler(CommandHandler("unblock", unblock_user))
        dispatcher.add_handler(CommandHandler("refund", refund))
        dispatcher.add_handler(CommandHandler("stats", stats))
        dispatcher.add_handler(CallbackQueryHandler(handle_callback))

        # Start cleanup job
        job_queue = updater.job_queue
        job_queue.run_repeating(cleanup_old_transactions, interval=CLEANUP_INTERVAL)

        # Start the bot
        updater.start_polling()
        updater.idle()

    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        logger.error(traceback.format_exc())
        raise

if __name__ == '__main__':
    main() 