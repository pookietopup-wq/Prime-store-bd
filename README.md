# PRIME STORE — Render starter project

## Before deployment
Set credentials in Render → your service → Environment (do not edit `bot.py`):
- `BOT_TOKEN` = Telegram bot token from @BotFather
- `API_KEY` = supplier API key (the code also accepts `SUPPLIER_API_KEY`)

For safety, rotate credentials that were posted in chat before deploying.
Owner ID is `7507661716`; the second admin ID `8985828566` is included automatically.

## Deploy to Render
1. Upload these files to a private GitHub repository:
   - `bot.py`
   - `requirements.txt`
   - `Procfile`
   - `runtime.txt`
2. Create a Render **Web Service** connected to that repository.
3. Choose Python/runtime as detected by Render.
4. The start command is `python bot.py` (Procfile already supplies it).
5. For persistent database storage, attach a persistent disk and set `DB_PATH` to a path on that disk, for example `/var/data/prime_store.db`. Mount the disk at `/var/data`.
   Without persistent storage, local SQLite data may be lost on redeploy/restart.
6. Deploy, then open the service URL; `/health` should return `{"status":"healthy"}`.

## How to use
- `/start` — user menu
- Admin Panel appears for owner ID `7507661716` and additional admin ID `8985828566`
- Add payment method: Admin Panel → Payment Methods → Add; format `USDT | TRC20 | wallet_address`
- Add own product: Admin Panel → Add Own Product; format `Title | Price_USDT | Description`
- Add stock: `/addstock PRODUCT_ID delivery_content` (one stock item per command)
- Add/remove admin: `/addadmin USER_ID`, `/deladmin USER_ID` (owner only)
- API profit: Admin Panel → API Profit; select percentage/fixed mode and enter value
- Force join: Admin Panel → Force Join

## Important limitations to validate
- Supplier API documentation only specifies `products`, `balance`, and `order`. Actual JSON field names, pagination, delivery fields, and error formats are not documented in the supplied text. The bot uses flexible field detection, but the product parser/delivery mapping may need adjustment after testing against real API responses.
- Manual USDT deposit approvals do **not** independently verify blockchain transactions. Admin must verify the TXID/amount/network in a trusted wallet/explorer before approving.
- Supplier API order errors are held for admin review rather than auto-refunded, to avoid duplicate orders if the supplier accepted the request before a timeout.
- This is a starter implementation, not a guarantee that every undocumented API response shape will work without testing.


Additional admin configured in this build: `8985828566`. Credentials are read from Render Environment (`BOT_TOKEN` and `API_KEY`, with `SUPPLIER_API_KEY` accepted as an alternative). The product parser supports common response shapes and displays all products returned in the `action=products` response, in batches of eight per message. If the supplier API uses pagination or a different undocumented response schema, live testing may require a provider-specific adjustment. The documented API routes are wired into the project; live behavior cannot be confirmed without valid credentials and a real supplier response.
