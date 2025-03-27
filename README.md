# Telegram Escrow Bot

A secure escrow bot for Telegram that facilitates cryptocurrency transactions between buyers and sellers.

## Features

- Buyer and seller role assignment with cryptocurrency addresses
- Transaction monitoring and confirmation
- Group-only functionality
- Personal chat commands (/start, /help, /links, /vouches)
- Transaction status updates
- Admin panel for managing transactions and users
- NOWPayments integration for BTC/LTC payments
- 5% escrow fee handling

## Setup

1. Clone this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your configuration:
   ```
   BOT_TOKEN=your_bot_token_here
   NOWPAYMENTS_API_KEY=your_nowpayments_api_key_here
   BOT_OWNER_ID=your_telegram_id_here
   ADMIN_IDS=admin1_id,admin2_id,admin3_id
   ADMIN_GROUP_ID=your_admin_group_id_here
   ESCROW_FEE_PERCENTAGE=5
   ```
4. Run the bot:
   ```bash
   python bot.py
   ```

## Commands

### Group Commands
- `/buyer <address>` - Set buyer role with crypto address
- `/seller <address>` - Set seller role with crypto address
- `/transaction <id>` - Check transaction status
- `/release` - Release funds to the other party
- `/admin` - Call for admin help

### Personal Chat Commands
- `/start` - Start the bot
- `/help` - Show help information
- `/links` - View owner and admin links
- `/vouches` - View vouch channel

### Admin Commands
- `/block <user_id>` - Block a user from using the bot
- `/unblock <user_id>` - Unblock a user
- `/refund <transaction_id>` - Initiate a refund
- `/stats` - View bot statistics

## Deployment

This bot is configured for deployment on Railway.app:

1. Push this code to GitHub
2. Create a new project on Railway.app
3. Connect your GitHub repository
4. Add the required environment variables
5. Deploy!

## Security

- Bot requires admin privileges in groups
- All transactions are monitored on the blockchain
- Transaction confirmations are required for completion
- Admin controls for managing bad actors
- Escrow fee handling for secure transactions 