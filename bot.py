import os
import json
import time
import sqlite3
import threading
import logging
import uuid
from decimal import Decimal, InvalidOperation, ROUND_UP
from functools import wraps

import requests
import telebot
from telebot import types
from flask import Flask, jsonify

# =========================
# PRIME STORE configuration
# =========================
# Set these in Render → your service → Environment.
# BOT_TOKEN = your Telegram bot token
# API_KEY (or SUPPLIER_API_KEY) = supplier API key
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPPLIER_API_KEY = os.getenv("API_KEY", os.getenv("SUPPLIER_API_KEY", "")).strip()
SUPPLIER_API_URL = "https://ssondigitalworks.online/api/reseller"
OWNER_ID = 7507661716
EXTRA_ADMIN_IDS = {8985828566}  # Additional admin requested by owner

DB_PATH = os.getenv("DB_PATH", "prime_store.db")
POLLING_TIMEOUT = 30
REQUEST_TIMEOUT = 25
CURRENCY = "USDT"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("prime-store")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    log.warning("Set BOT_TOKEN in Render Environment before starting.")
if not SUPPLIER_API_KEY:
    log.warning("Set API_KEY (or SUPPLIER_API_KEY) in Render Environment before using supplier API.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
app = Flask(__name__)
session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {SUPPLIER_API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "PrimeStoreTelegramBot/1.0",
})

# -------------------------
# Database
# -------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, balance REAL NOT NULL DEFAULT 0,
            referred_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            method TEXT NOT NULL, amount REAL NOT NULL, txid TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS own_products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT DEFAULT '',
            price REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS own_stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
            payload TEXT NOT NULL, sold INTEGER NOT NULL DEFAULT 0, order_id TEXT,
            FOREIGN KEY(product_id) REFERENCES own_products(id)
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            kind TEXT NOT NULL, product_id TEXT NOT NULL, product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL, unit_price REAL NOT NULL, total REAL NOT NULL,
            status TEXT NOT NULL, external_order_id TEXT UNIQUE,
            supplier_response TEXT DEFAULT '', delivery TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS force_channels(
            channel TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS payment_methods(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, network TEXT NOT NULL,
            address TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
        );
        """)
        c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (OWNER_ID,))
        for admin_id in EXTRA_ADMIN_IDS:
            c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (admin_id,))
        defaults = {
            "api_profit_mode": "percent",
            "api_profit_value": "20",
            "referral_percent": "0",
            "store_name": "PRIME STORE",
            "min_deposit": "1",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

def get_setting(key, default=""):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))

def is_admin(uid):
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

def ensure_user(user):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,username) VALUES(?,?)",
                  (user.id, user.username or "" ))
        c.execute("UPDATE users SET username=? WHERE user_id=?", (user.username or "", user.id))

def money(n):
    try:
        return f"{Decimal(str(n)).quantize(Decimal('0.01')):.2f}"
    except Exception:
        return str(n)

def parse_json_response(resp):
    try:
        data = resp.json()
        return data
    except Exception:
        return {"_raw_text": resp.text[:3000], "_http_status": resp.status_code}

def api_request(method, action, payload=None):
    if not SUPPLIER_API_KEY:
        raise RuntimeError("Supplier API key is missing. Set API_KEY in Render Environment.")
    url = SUPPLIER_API_URL + "?action=" + action
    try:
        r = session.request(method, url, json=payload, timeout=REQUEST_TIMEOUT)
        data = parse_json_response(r)
        if r.status_code >= 400:
            raise RuntimeError(f"Supplier API HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:700]}")
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(str(data.get("message") or data.get("error") or "Supplier API returned success=false"))
        return data
    except requests.RequestException as e:
        raise RuntimeError(f"Supplier API connection error: {e}")

def flatten_products(data):
    """Extract product records from common supplier API JSON response shapes.

    The documented endpoint is action=products. If the provider uses a different
    schema or pagination, that must be confirmed from a real response.
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    preferred = ("products", "items", "catalog", "result", "data", "services")
    for key in preferred:
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            nested = flatten_products(val)
            if nested:
                return nested
    # Some APIs wrap the payload under a generic response/result key.
    for val in data.values():
        if isinstance(val, dict):
            nested = flatten_products(val)
            if nested:
                return nested
    return []

def pick(d, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return default

def product_fields(p):
    pid = pick(p, "product_id", "productId", "productID", "id", "pid", "code", "sku", "service_id", "service")
    name = pick(p, "name", "title", "product_name", "productName", "product", "label", "service_name", "serviceName")
    cost = pick(p, "price", "rate", "cost", "wholesale_price", "wholesalePrice", "api_price", "amount", "selling_price")
    description = pick(p, "description", "details", "desc", "note", default="")
    if pid is None or name is None or cost is None:
        return None
    try:
        cost = Decimal(str(cost))
        if cost < 0:
            return None
    except (InvalidOperation, ValueError):
        return None
    return str(pid), str(name), cost, str(description)

def api_selling_price(cost):
    try:
        v = Decimal(get_setting("api_profit_value", "20"))
        mode = get_setting("api_profit_mode", "percent")
        price = cost * (Decimal("1") + v / Decimal("100")) if mode == "percent" else cost + v
        return price.quantize(Decimal("0.01"), rounding=ROUND_UP)
    except Exception:
        return (cost * Decimal("1.20")).quantize(Decimal("0.01"), rounding=ROUND_UP)

def user_balance(uid):
    with db() as c:
        r = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        return float(r["balance"]) if r else 0.0

def update_balance(uid, delta):
    with db() as c:
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (float(delta), uid))

def safe_html(s):
    import html
    return html.escape(str(s or ""))

# -------------------------
# Keyboards / navigation
# -------------------------
def main_keyboard(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🛍️ Products", "📁 Own Products")
    kb.add("💰 Deposit", "👛 Wallet")
    kb.add("📦 My Orders", "🎁 Referral")
    kb.add("🎧 Support", "ℹ️ About")
    if is_admin(uid):
        kb.add("🛠️ Admin Panel")
    return kb

def admin_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    items = [
        ("📦 API Products", "admin_api_products"),
        ("💳 Payment Methods", "admin_payments"),
        ("📢 Force Join", "admin_force"),
        ("➕ Add Own Product", "admin_add_product"),
        ("📋 Own Products", "admin_own_products"),
        ("📈 API Profit", "admin_profit"),
        ("💰 API Balance", "admin_api_balance"),
        ("🧾 Pending Deposits", "admin_pending"),
        ("👥 Admins", "admin_admins"),
        ("📊 Statistics", "admin_stats"),
    ]
    for label, data in items:
        kb.add(types.InlineKeyboardButton(label, callback_data=data))
    return kb

def force_join_missing(uid):
    with db() as c:
        channels = [r["channel"] for r in c.execute("SELECT channel FROM force_channels").fetchall()]
    missing = []
    for ch in channels:
        try:
            member = bot.get_chat_member(ch, uid)
            if member.status not in ("member", "administrator", "creator"):
                missing.append(ch)
        except Exception:
            # Fail closed if channel cannot be checked; admin must add bot to channel.
            missing.append(ch)
    return missing

def send_force_join(chat_id, missing):
    kb = types.InlineKeyboardMarkup()
    for ch in missing:
        link = ch if ch.startswith("https://") else "https://t.me/" + ch.lstrip("@")
        kb.add(types.InlineKeyboardButton("Join " + ch, url=link))
    kb.add(types.InlineKeyboardButton("✅ Joined / Check", callback_data="check_join"))
    bot.send_message(chat_id, "🔒 আগে নিচের channel-গুলোতে join করুন, তারপর Check চাপুন:\n\n" +
                     "\n".join("• " + safe_html(x) for x in missing), reply_markup=kb)

def admin_only(func):
    @wraps(func)
    def wrapper(call_or_message, *args, **kwargs):
        uid = call_or_message.from_user.id
        if not is_admin(uid):
            if isinstance(call_or_message, types.CallbackQuery):
                bot.answer_callback_query(call_or_message.id, "Admin only.", show_alert=True)
            else:
                bot.reply_to(call_or_message, "⛔ Admin only.")
            return
        return func(call_or_message, *args, **kwargs)
    return wrapper

# -------------------------
# Start and user commands
# -------------------------
@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message.from_user)
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref = int(args[1][4:])
            if ref != message.from_user.id:
                with db() as c:
                    row = c.execute("SELECT referred_by FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
                    if row and row["referred_by"] is None and c.execute("SELECT 1 FROM users WHERE user_id=?", (ref,)).fetchone():
                        c.execute("UPDATE users SET referred_by=? WHERE user_id=?", (ref, message.from_user.id))
        except Exception:
            pass
    missing = force_join_missing(message.from_user.id)
    if missing:
        send_force_join(message.chat.id, missing)
        return
    bot.send_message(message.chat.id,
        f"👑 <b>{safe_html(get_setting('store_name', 'PRIME STORE'))}</b>\n\n"
        "Welcome! Choose an option below.",
        reply_markup=main_keyboard(message.from_user.id))

@bot.callback_query_handler(func=lambda c: c.data == "check_join")
def check_join(call):
    missing = force_join_missing(call.from_user.id)
    if missing:
        bot.answer_callback_query(call.id, "সব channel join করা হয়নি।", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Verified!")
    bot.send_message(call.message.chat.id, "Welcome to PRIME STORE!", reply_markup=main_keyboard(call.from_user.id))

@bot.message_handler(func=lambda m: m.text == "👛 Wallet")
def wallet(message):
    ensure_user(message.from_user)
    bot.send_message(message.chat.id, f"👛 Your balance: <b>{money(user_balance(message.from_user.id))} USDT</b>",
                     reply_markup=main_keyboard(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == "💰 Deposit")
def deposit(message):
    ensure_user(message.from_user)
    with db() as c:
        methods = c.execute("SELECT * FROM payment_methods WHERE enabled=1").fetchall()
    if not methods:
        bot.send_message(message.chat.id, "এখন কোনো payment method চালু নেই। Admin Panel থেকে যোগ করা হবে।")
        return
    kb = types.InlineKeyboardMarkup()
    for m in methods:
        kb.add(types.InlineKeyboardButton(f"{m['name']} ({m['network']})", callback_data=f"deposit_method:{m['id']}"))
    bot.send_message(message.chat.id, "Deposit method নির্বাচন করুন:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("deposit_method:"))
def choose_deposit_method(call):
    try:
        mid = int(call.data.split(":")[1])
        with db() as c:
            m = c.execute("SELECT * FROM payment_methods WHERE id=? AND enabled=1", (mid,)).fetchone()
        if not m:
            return bot.answer_callback_query(call.id, "Payment method unavailable.", show_alert=True)
        msg = bot.send_message(call.message.chat.id,
            f"💳 <b>{safe_html(m['name'])} — {safe_html(m['network'])}</b>\n"
            f"Wallet address:\n<code>{safe_html(m['address'])}</code>\n\n"
            "এখন এক মেসেজে amount (USDT) ও TXID দিন এই ফরম্যাটে:\n<code>10 | transaction_hash</code>")
        bot.register_next_step_handler(msg, submit_deposit, mid)
    except Exception as e:
        log.exception("choose deposit: %s", e)

def submit_deposit(message, method_id):
    try:
        parts = (message.text or "").split("|", 1)
        if len(parts) != 2:
            return bot.reply_to(message, "Format ভুল। এভাবে দিন: <code>10 | transaction_hash</code>")
        amount = float(parts[0].strip())
        txid = parts[1].strip()
        if amount <= 0 or amount < float(get_setting("min_deposit", "1")) or len(txid) < 5:
            return bot.reply_to(message, "Amount বা TXID ঠিক নেই।")
        with db() as c:
            m = c.execute("SELECT * FROM payment_methods WHERE id=? AND enabled=1", (method_id,)).fetchone()
            if not m:
                return bot.reply_to(message, "Payment method বন্ধ আছে।")
            cur = c.execute("INSERT INTO payments(user_id,method,amount,txid) VALUES(?,?,?,?)",
                            (message.from_user.id, f"{m['name']} / {m['network']}", amount, txid))
            payment_id = cur.lastrowid
        bot.reply_to(message, f"✅ Deposit request #{payment_id} submitted. Admin verify করার পর balance add হবে।")
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ Approve", callback_data=f"dep_approve:{payment_id}"),
               types.InlineKeyboardButton("❌ Reject", callback_data=f"dep_reject:{payment_id}"))
        with db() as c:
            admins = [r["user_id"] for r in c.execute("SELECT user_id FROM admins").fetchall()]
        for aid in admins:
            try:
                bot.send_message(aid,
                    f"💰 <b>Deposit #{payment_id}</b>\nUser: <code>{message.from_user.id}</code>\n"
                    f"Amount: {money(amount)} USDT\nMethod: {safe_html(m['name'])} / {safe_html(m['network'])}\n"
                    f"TXID: <code>{safe_html(txid)}</code>", reply_markup=kb)
            except Exception:
                pass
    except Exception as e:
        log.exception("submit deposit: %s", e)
        bot.reply_to(message, "Deposit request failed. Try again.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("dep_approve:") or c.data.startswith("dep_reject:"))
def review_deposit(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    action, pid = call.data.split(":")
    pid = int(pid)
    with db() as c:
        p = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not p or p["status"] != "pending":
            return bot.answer_callback_query(call.id, "Already reviewed.", show_alert=True)
        status = "approved" if action == "dep_approve" else "rejected"
        c.execute("UPDATE payments SET status=?, reviewed_by=? WHERE id=?", (status, call.from_user.id, pid))
        if status == "approved":
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (p["amount"], p["user_id"]))
    try:
        bot.send_message(p["user_id"], f"Deposit #{pid} {status}. " +
                         (f"Added {money(p['amount'])} USDT to wallet." if status == "approved" else ""))
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Updated.")
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

@bot.message_handler(func=lambda m: m.text == "🛍️ Products")
def api_products(message):
    try:
        data = api_request("GET", "products")
        products = flatten_products(data)
        if not products:
            bot.send_message(message.chat.id,
                "Supplier API response এসেছে, কিন্তু product list format চিনতে পারিনি। Admin-কে API response format inspect করতে হবে.\n\n"
                f"<code>{safe_html(json.dumps(data, ensure_ascii=False)[:2500])}</code>")
            return
        # Send in pages of 8 products per message; show all products, limited to Telegram-safe output.
        bot.send_message(message.chat.id, f"🌐 Supplier catalog: {len(products)} products")
        for start_i in range(0, len(products), 8):
            kb = types.InlineKeyboardMarkup(row_width=1)
            lines = []
            for p in products[start_i:start_i+8]:
                f = product_fields(p)
                if not f:
                    continue
                pid, name, cost, desc = f
                sell = api_selling_price(cost)
                lines.append(f"• <b>{safe_html(name)}</b> — {money(sell)} USDT")
                kb.add(types.InlineKeyboardButton(f"Buy: {name[:45]} · {money(sell)} USDT",
                                                  callback_data=f"api_buy:{pid}"))
            if lines:
                bot.send_message(message.chat.id, "\n".join(lines), reply_markup=kb)
    except Exception as e:
        log.exception("API products: %s", e)
        bot.send_message(message.chat.id, f"Supplier API error:\n<code>{safe_html(str(e)[:1200])}</code>")

@bot.callback_query_handler(func=lambda c: c.data.startswith("api_buy:"))
def api_buy(call):
    uid = call.from_user.id
    pid = call.data.split(":", 1)[1]
    try:
        data = api_request("GET", "products")
        products = flatten_products(data)
        match = None
        for p in products:
            f = product_fields(p)
            if f and f[0] == pid:
                match = f
                break
        if not match:
            return bot.answer_callback_query(call.id, "Product no longer available.", show_alert=True)
        product_id, name, cost, desc = match
        sell = api_selling_price(cost)
        bal = Decimal(str(user_balance(uid)))
        if bal < sell:
            return bot.answer_callback_query(call.id, f"Insufficient wallet. Price {money(sell)} USDT.", show_alert=True)
        # Debit first in a transaction to prevent double spending; refund if supplier order fails.
        external_id = "PS-" + uuid.uuid4().hex[:20].upper()
        with db() as c:
            r = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
            if not r or Decimal(str(r["balance"])) < sell:
                return bot.answer_callback_query(call.id, "Insufficient balance.", show_alert=True)
            c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (float(sell), uid))
            cur = c.execute("""INSERT INTO orders(user_id,kind,product_id,product_name,quantity,unit_price,total,status,external_order_id)
                               VALUES(?,?,?,?,1,?,?,?,?)""",
                            (uid, "api", product_id, name, float(sell), float(sell), "processing", external_id))
            local_order_id = cur.lastrowid
        try:
            resp = api_request("POST", "order", {
                "product_id": product_id, "quantity": 1, "external_order_id": external_id
            })
            delivery = extract_delivery(resp)
            status = "completed" if delivery else supplier_status(resp)
            with db() as c:
                c.execute("UPDATE orders SET status=?, supplier_response=?, delivery=? WHERE id=?",
                          (status, json.dumps(resp, ensure_ascii=False)[:5000], delivery, local_order_id))
            bot.answer_callback_query(call.id, "Order placed.")
            txt = f"✅ Order #{local_order_id}\nProduct: <b>{safe_html(name)}</b>\nPrice: {money(sell)} USDT\nStatus: {safe_html(status)}"
            if delivery:
                txt += "\n\n📦 Delivery:\n<code>" + safe_html(delivery[:3000]) + "</code>"
            else:
                txt += "\n\nSupplier response:\n<code>" + safe_html(json.dumps(resp, ensure_ascii=False)[:1800]) + "</code>\n\nDelivery/status details may require supplier response mapping."
            bot.send_message(call.message.chat.id, txt)
        except Exception as e:
            # Supplier could have accepted order before network error. Do NOT auto-refund blindly.
            with db() as c:
                c.execute("UPDATE orders SET status='needs_review', supplier_response=? WHERE id=?",
                          (str(e)[:2000], local_order_id))
            bot.answer_callback_query(call.id, "Order needs admin review.", show_alert=True)
            bot.send_message(call.message.chat.id,
                f"⚠️ Order #{local_order_id} needs review. Balance is held to avoid duplicate supplier charge. Contact support.")
    except Exception as e:
        log.exception("API order: %s", e)
        bot.answer_callback_query(call.id, "Could not process order.", show_alert=True)

def supplier_status(resp):
    if isinstance(resp, dict):
        status = str(pick(resp, "status", "order_status", default="processing")).lower()
        if status in ("success", "completed", "complete", "delivered", "done"):
            return "completed"
        if status in ("failed", "error", "rejected", "cancelled", "canceled"):
            return "failed"
    return "processing"

def extract_delivery(resp):
    if not isinstance(resp, dict):
        return ""
    keys = ("delivery", "delivered_data", "account", "credentials", "content", "file", "result_data", "data")
    for k in keys:
        val = resp.get(k)
        if isinstance(val, (str, int, float)) and str(val).strip():
            # Ignore generic nested status messages.
            if k == "data" and isinstance(val, str) and val.strip().lower() in ("success", "ok", "completed"):
                continue
            return str(val)
        if isinstance(val, dict):
            for sub in ("delivery", "account", "credentials", "content", "file", "key", "code"):
                if val.get(sub):
                    return str(val[sub])
    return ""

@bot.message_handler(func=lambda m: m.text == "📁 Own Products")
def own_products(message):
    with db() as c:
        products = c.execute("SELECT p.*, (SELECT COUNT(*) FROM own_stock s WHERE s.product_id=p.id AND s.sold=0) AS stock FROM own_products p WHERE active=1 ORDER BY id DESC").fetchall()
    if not products:
        return bot.send_message(message.chat.id, "এখন নিজের কোনো product নেই। Admin Panel থেকে add করা যাবে।")
    for p in products:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(f"Buy · {money(p['price'])} USDT", callback_data=f"own_buy:{p['id']}"))
        bot.send_message(message.chat.id,
            f"📦 <b>{safe_html(p['title'])}</b>\n{safe_html(p['description'])}\n"
            f"Price: {money(p['price'])} USDT\nStock: {p['stock']}", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("own_buy:"))
def own_buy(call):
    uid = call.from_user.id
    pid = int(call.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM own_products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            return bot.answer_callback_query(call.id, "Product unavailable.", show_alert=True)
        bal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if not bal or float(bal["balance"]) < p["price"]:
            return bot.answer_callback_query(call.id, f"Insufficient wallet. Price {money(p['price'])} USDT.", show_alert=True)
        stock = c.execute("SELECT * FROM own_stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1", (pid,)).fetchone()
        if not stock:
            return bot.answer_callback_query(call.id, "Out of stock.", show_alert=True)
        c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (p["price"], uid))
        order_id = "OWN-" + uuid.uuid4().hex[:16].upper()
        c.execute("UPDATE own_stock SET sold=1, order_id=? WHERE id=?", (order_id, stock["id"]))
        c.execute("""INSERT INTO orders(user_id,kind,product_id,product_name,quantity,unit_price,total,status,external_order_id,delivery)
                     VALUES(?,?,?,?,1,?,?, 'completed', ?, ?)""",
                  (uid, "own", str(pid), p["title"], p["price"], p["price"], order_id, stock["payload"]))
    bot.answer_callback_query(call.id, "Delivered!")
    bot.send_message(call.message.chat.id,
        f"✅ <b>Order completed!</b>\nProduct: {safe_html(p['title'])}\n"
        f"Delivery:\n<code>{safe_html(stock['payload'][:3500])}</code>")

@bot.message_handler(func=lambda m: m.text == "📦 My Orders")
def my_orders(message):
    with db() as c:
        rows = c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15", (message.from_user.id,)).fetchall()
    if not rows:
        return bot.send_message(message.chat.id, "কোনো order নেই।")
    out = []
    for r in rows:
        out.append(f"#{r['id']} · {safe_html(r['product_name'])}\n{money(r['total'])} USDT · {safe_html(r['status'])} · {r['created_at']}")
    bot.send_message(message.chat.id, "\n\n".join(out))

@bot.message_handler(func=lambda m: m.text == "🎁 Referral")
def referral(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id,
        f"🎁 Your referral link:\n<code>https://t.me/{bot.get_me().username}?start=ref_{uid}</code>\n"
        f"Referral reward: {get_setting('referral_percent','0')}% (configured by admin; payout automation not enabled yet).")

@bot.message_handler(func=lambda m: m.text == "🎧 Support")
def support(message):
    bot.send_message(message.chat.id, "Support: contact the store admin.")

@bot.message_handler(func=lambda m: m.text == "ℹ️ About")
def about(message):
    bot.send_message(message.chat.id, "👑 PRIME STORE\nDigital products from supplier API and store stock.")

@bot.message_handler(func=lambda m: m.text == "🛠️ Admin Panel")
def admin_panel(message):
    if not is_admin(message.from_user.id):
        return
    bot.send_message(message.chat.id, "🛠️ <b>PRIME STORE Admin Panel</b>", reply_markup=admin_keyboard())

# -------------------------
# Admin callbacks
# -------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def admin_callbacks(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    action = call.data
    if action == "admin_api_products":
        try:
            data = api_request("GET", "products")
            products = flatten_products(data)
            bot.send_message(call.message.chat.id,
                f"Supplier API returned {len(products)} product entries.\n"
                f"Raw response preview:\n<code>{safe_html(json.dumps(data, ensure_ascii=False)[:3500])}</code>")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"API error: <code>{safe_html(e)}</code>")
    elif action == "admin_payments":
        with db() as c:
            rows = c.execute("SELECT * FROM payment_methods ORDER BY id").fetchall()
        kb = types.InlineKeyboardMarkup()
        for m in rows:
            kb.add(types.InlineKeyboardButton(
                f"{'🟢' if m['enabled'] else '⚪'} {m['name']} / {m['network']} (#{m['id']})",
                callback_data=f"pm_toggle:{m['id']}"))
        kb.add(types.InlineKeyboardButton("➕ Add payment method", callback_data="pm_add"))
        bot.send_message(call.message.chat.id, "Payment methods (tap to enable/disable):", reply_markup=kb)
    elif action == "admin_force":
        with db() as c:
            channels = [r["channel"] for r in c.execute("SELECT channel FROM force_channels").fetchall()]
        kb = types.InlineKeyboardMarkup()
        for ch in channels:
            kb.add(types.InlineKeyboardButton("🗑️ Remove " + ch, callback_data="force_remove:" + ch))
        kb.add(types.InlineKeyboardButton("➕ Add channel", callback_data="force_add"))
        bot.send_message(call.message.chat.id, "Force-join channels:\n" + ("\n".join(channels) if channels else "None"), reply_markup=kb)
    elif action == "admin_add_product":
        msg = bot.send_message(call.message.chat.id,
            "নিজের product add করতে এই format দিন:\n<code>Title | Price_USDT | Description</code>")
        bot.register_next_step_handler(msg, admin_add_product_step)
    elif action == "admin_own_products":
        with db() as c:
            rows = c.execute("SELECT p.*, (SELECT COUNT(*) FROM own_stock s WHERE s.product_id=p.id AND s.sold=0) stock FROM own_products p ORDER BY id DESC LIMIT 50").fetchall()
        if not rows:
            return bot.send_message(call.message.chat.id, "No own products yet.")
        kb = types.InlineKeyboardMarkup()
        for p in rows:
            kb.add(types.InlineKeyboardButton(f"#{p['id']} {p['title']} · stock {p['stock']}", callback_data=f"own_manage:{p['id']}"))
        bot.send_message(call.message.chat.id, "Own products:", reply_markup=kb)
    elif action == "admin_profit":
        mode = get_setting("api_profit_mode", "percent")
        value = get_setting("api_profit_value", "20")
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("Percentage", callback_data="profit_mode:percent"),
               types.InlineKeyboardButton("Fixed USDT", callback_data="profit_mode:fixed"))
        msg = bot.send_message(call.message.chat.id,
            f"Current API profit: {value} ({mode})\nSend a number to set the profit value.")
        bot.register_next_step_handler(msg, admin_profit_value_step)
        bot.send_message(call.message.chat.id, "Choose profit mode:", reply_markup=kb)
    elif action == "admin_api_balance":
        try:
            data = api_request("GET", "balance")
            bot.send_message(call.message.chat.id, "Supplier API balance:\n<code>" + safe_html(json.dumps(data, ensure_ascii=False)[:2500]) + "</code>")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"API error: <code>{safe_html(e)}</code>")
    elif action == "admin_pending":
        with db() as c:
            rows = c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT 20").fetchall()
        if not rows:
            return bot.send_message(call.message.chat.id, "No pending deposits.")
        for p in rows:
            kb = types.InlineKeyboardMarkup()
            kb.row(types.InlineKeyboardButton("✅ Approve", callback_data=f"dep_approve:{p['id']}"),
                   types.InlineKeyboardButton("❌ Reject", callback_data=f"dep_reject:{p['id']}"))
            bot.send_message(call.message.chat.id,
                f"Deposit #{p['id']} · user {p['user_id']} · {money(p['amount'])} USDT\n"
                f"{safe_html(p['method'])}\nTXID: <code>{safe_html(p['txid'])}</code>", reply_markup=kb)
    elif action == "admin_admins":
        with db() as c:
            rows = [r["user_id"] for r in c.execute("SELECT user_id FROM admins").fetchall()]
        msg = bot.send_message(call.message.chat.id, "Admins: " + ", ".join(map(str, rows)) +
            "\nTo add admin, send: <code>/addadmin USER_ID</code>\nTo remove: <code>/deladmin USER_ID</code>")
    elif action == "admin_stats":
        with db() as c:
            users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
            sales = c.execute("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status='completed'").fetchone()["s"]
        bot.send_message(call.message.chat.id, f"Users: {users}\nOrders: {orders}\nCompleted sales: {money(sales)} USDT")
    bot.answer_callback_query(call.id)

def admin_add_product_step(message):
    if not is_admin(message.from_user.id):
        return
    try:
        title, price, desc = [x.strip() for x in message.text.split("|", 2)]
        price = float(price)
        if price <= 0:
            raise ValueError()
        with db() as c:
            cur = c.execute("INSERT INTO own_products(title,price,description) VALUES(?,?,?)", (title, price, desc))
            pid = cur.lastrowid
        bot.send_message(message.chat.id,
            f"Product #{pid} added. Now add stock with:\n<code>/addstock {pid} delivery_content</code>\n"
            "For multiple stock items, run the command once per item.")
    except Exception:
        bot.reply_to(message, "Format ভুল। উদাহরণ: <code>Premium Account | 5 | Details</code>")

def admin_profit_value_step(message):
    if not is_admin(message.from_user.id):
        return
    try:
        val = Decimal(message.text.strip())
        if val < 0:
            raise ValueError()
        set_setting("api_profit_value", str(val))
        bot.reply_to(message, f"API profit value saved: {val}. Current mode: {get_setting('api_profit_mode','percent')}")
    except Exception:
        bot.reply_to(message, "শুধু 0 বা positive number দিন।")

@bot.callback_query_handler(func=lambda c: c.data.startswith("profit_mode:"))
def profit_mode(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    mode = call.data.split(":")[1]
    if mode not in ("percent", "fixed"):
        return
    set_setting("api_profit_mode", mode)
    bot.answer_callback_query(call.id, "Profit mode updated.")
    bot.send_message(call.message.chat.id, "Profit mode set to " + ("percentage" if mode == "percent" else "fixed USDT"))

@bot.callback_query_handler(func=lambda c: c.data == "pm_add")
def pm_add(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    msg = bot.send_message(call.message.chat.id, "এই format দিন:\n<code>Name | Network | WalletAddress</code>")
    bot.register_next_step_handler(msg, pm_add_step)
    bot.answer_callback_query(call.id)

def pm_add_step(message):
    if not is_admin(message.from_user.id):
        return
    try:
        name, network, address = [x.strip() for x in message.text.split("|", 2)]
        with db() as c:
            c.execute("INSERT INTO payment_methods(name,network,address) VALUES(?,?,?)", (name, network, address))
        bot.reply_to(message, "Payment method added.")
    except Exception:
        bot.reply_to(message, "Format ভুল: <code>USDT | TRC20 | wallet_address</code>")

@bot.callback_query_handler(func=lambda c: c.data.startswith("pm_toggle:"))
def pm_toggle(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    mid = int(call.data.split(":")[1])
    with db() as c:
        row = c.execute("SELECT enabled FROM payment_methods WHERE id=?", (mid,)).fetchone()
        if row:
            c.execute("UPDATE payment_methods SET enabled=? WHERE id=?", (0 if row["enabled"] else 1, mid))
    bot.answer_callback_query(call.id, "Updated.")
    admin_callbacks_proxy(call.message.chat.id, "admin_payments")

def admin_callbacks_proxy(chat_id, action):
    # Recreate the payment list without fabricating a callback object.
    with db() as c:
        rows = c.execute("SELECT * FROM payment_methods ORDER BY id").fetchall()
    kb = types.InlineKeyboardMarkup()
    for m in rows:
        kb.add(types.InlineKeyboardButton(
            f"{'🟢' if m['enabled'] else '⚪'} {m['name']} / {m['network']} (#{m['id']})",
            callback_data=f"pm_toggle:{m['id']}"))
    kb.add(types.InlineKeyboardButton("➕ Add payment method", callback_data="pm_add"))
    bot.send_message(chat_id, "Payment methods:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "force_add")
def force_add(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    msg = bot.send_message(call.message.chat.id, "Channel username বা public link দিন, যেমন @mychannel")
    bot.register_next_step_handler(msg, force_add_step)
    bot.answer_callback_query(call.id)

def force_add_step(message):
    if not is_admin(message.from_user.id):
        return
    ch = (message.text or "").strip()
    if not ch:
        return bot.reply_to(message, "Channel দিন।")
    with db() as c:
        c.execute("INSERT OR IGNORE INTO force_channels(channel) VALUES(?)", (ch,))
    bot.reply_to(message, "Force-join channel added. Bot-কে channel-এ admin করুন, নইলে membership check কাজ নাও করতে পারে.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("force_remove:"))
def force_remove(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    ch = call.data.split(":", 1)[1]
    with db() as c:
        c.execute("DELETE FROM force_channels WHERE channel=?", (ch,))
    bot.answer_callback_query(call.id, "Removed.")
    bot.send_message(call.message.chat.id, f"Removed {safe_html(ch)}")

@bot.message_handler(commands=["addstock"])
def add_stock(message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, pid, payload = message.text.split(maxsplit=2)
        with db() as c:
            p = c.execute("SELECT id FROM own_products WHERE id=?", (int(pid),)).fetchone()
            if not p:
                return bot.reply_to(message, "Product ID not found.")
            c.execute("INSERT INTO own_stock(product_id,payload) VALUES(?,?)", (int(pid), payload))
        bot.reply_to(message, f"Stock added to product #{pid}.")
    except Exception:
        bot.reply_to(message, "Usage: <code>/addstock PRODUCT_ID delivery_content</code>")

@bot.message_handler(commands=["addadmin"])
def add_admin(message):
    if message.from_user.id != OWNER_ID:
        return
    try:
        uid = int(message.text.split()[1])
        with db() as c:
            c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
        bot.reply_to(message, f"Admin {uid} added.")
    except Exception:
        bot.reply_to(message, "Usage: /addadmin USER_ID")

@bot.message_handler(commands=["deladmin"])
def del_admin(message):
    if message.from_user.id != OWNER_ID:
        return
    try:
        uid = int(message.text.split()[1])
        if uid == OWNER_ID:
            return bot.reply_to(message, "Owner cannot be removed.")
        with db() as c:
            c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        bot.reply_to(message, f"Admin {uid} removed.")
    except Exception:
        bot.reply_to(message, "Usage: /deladmin USER_ID")

@app.get("/")
def health():
    return jsonify({"status": "ok", "bot": "PRIME STORE"})

@app.get("/health")
def health2():
    return jsonify({"status": "healthy"})

def run_web():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def main():
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN in Render Environment.")
    if not SUPPLIER_API_KEY:
        log.warning("Supplier API calls will fail until API_KEY is set in Render Environment.")
    log.info("Starting PRIME STORE polling...")
    while True:
        try:
            bot.infinity_polling(timeout=POLLING_TIMEOUT, long_polling_timeout=POLLING_TIMEOUT,
                                 skip_pending=True, allowed_updates=["message", "callback_query"])
        except Exception:
            log.exception("Polling stopped; restarting in 5 seconds")
            time.sleep(5)

if __name__ == "__main__":
    main()
