import os
import re
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Optional, List, Tuple

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("sol_watch_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
DB_PATH = os.getenv("DB_PATH", "data.db").strip()
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))

# =========================
# Conversation states
# =========================
ASK_NAME, ASK_ADDRESS = range(2)

# =========================
# Callback data keys
# =========================
CB_ADD = "add"
CB_REFRESH = "refresh"
CB_BACK_MENU = "back_menu"

CB_WALLETS = "wallets"
CB_ALERTS = "alerts"
CB_SETTINGS = "settings"
CB_HELP = "help"

CB_WALLETS_PAGE_PREFIX = "wpage:"     # wpage:<n>
CB_TOGGLE_PREFIX = "toggle:"          # toggle:<wallet_id>
CB_DELETE_PREFIX = "delete:"          # delete:<wallet_id>

# =========================
# Address validation (rough)
# =========================
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

def is_probably_solana_address(addr: str) -> bool:
    return bool(BASE58_RE.match(addr.strip()))

# =========================
# Database helpers (SQLite)
# =========================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def db_init() -> None:
    conn = db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_lamports INTEGER,
                UNIQUE(user_id, address)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallets_user_id ON wallets(user_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallets_enabled ON wallets(enabled);")
        conn.commit()
    finally:
        conn.close()

def db_add_wallet(user_id: int, name: str, address: str) -> Tuple[bool, str]:
    conn = db_connect()
    try:
        try:
            conn.execute(
                "INSERT INTO wallets (user_id, name, address, enabled, last_lamports) VALUES (?, ?, ?, 1, NULL)",
                (user_id, name, address),
            )
            conn.commit()
            return True, "✅ Wallet saved!"
        except sqlite3.IntegrityError:
            return False, "⚠️ That wallet address is already saved for you."
    finally:
        conn.close()

def db_get_wallets(user_id: int) -> List[Tuple[int, str, str, int, Optional[int]]]:
    conn = db_connect()
    try:
        cur = conn.execute(
            "SELECT id, name, address, enabled, last_lamports FROM wallets WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        )
        return cur.fetchall()
    finally:
        conn.close()

def db_toggle_wallet(user_id: int, wallet_id: int) -> Optional[int]:
    conn = db_connect()
    try:
        cur = conn.execute(
            "SELECT enabled FROM wallets WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        enabled = int(row[0])
        new_enabled = 0 if enabled == 1 else 1
        conn.execute(
            "UPDATE wallets SET enabled = ? WHERE id = ? AND user_id = ?",
            (new_enabled, wallet_id, user_id),
        )
        conn.commit()
        return new_enabled
    finally:
        conn.close()

def db_delete_wallet(user_id: int, wallet_id: int) -> bool:
    conn = db_connect()
    try:
        cur = conn.execute(
            "DELETE FROM wallets WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def db_get_enabled_wallets_all_users() -> List[Tuple[int, int, str, str, Optional[int]]]:
    """
    Returns: (wallet_id, user_id, name, address, last_lamports)
    """
    conn = db_connect()
    try:
        cur = conn.execute(
            "SELECT id, user_id, name, address, last_lamports FROM wallets WHERE enabled = 1"
        )
        return cur.fetchall()
    finally:
        conn.close()

def db_update_last_lamports(wallet_id: int, lamports: int) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE wallets SET last_lamports = ? WHERE id = ?",
            (lamports, wallet_id),
        )
        conn.commit()
    finally:
        conn.close()

# =========================
# Solana RPC helpers
# =========================
async def rpc_get_balance_lamports(session: aiohttp.ClientSession, address: str) -> int:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address],
    }
    async with session.post(
        SOLANA_RPC_URL,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=12),
    ) as resp:
        data = await resp.json(content_type=None)
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return int(data["result"]["value"])

def lamports_to_sol(lamports: int) -> float:
    return lamports / 1_000_000_000

def truncate_addr(a: str) -> str:
    a = a.strip()
    if len(a) <= 10:
        return a
    return f"{a[:4]}…{a[-4:]}"

# =========================
# Modern UI (Dashboard + Screens)
# =========================
def dashboard_text(wallet_rows: List[Tuple[int, str, str, int, Optional[int]]], last_check: Optional[str]) -> str:
    total = len(wallet_rows)
    enabled = sum(1 for _id, _n, _a, en, _l in wallet_rows if en == 1)
    disabled = total - enabled

    last_line = last_check or "—"

    return (
        "🌊 *Sol Watch*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *Dashboard*\n\n"
        f"👛 *Wallets:* {total}\n"
        f"🔔 *Alerts On:* {enabled}\n"
        f"🔕 *Alerts Off:* {disabled}\n\n"
        f"🕒 *Last scan:* {last_line}\n\n"
        "⚡ Choose an option below:"
    )

def main_menu_keyboard_modern() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Add Wallet", callback_data=CB_ADD),
            InlineKeyboardButton("👛 My Wallets", callback_data=CB_WALLETS),
        ],
        [
            InlineKeyboardButton("🔔 Alerts", callback_data=CB_ALERTS),
            InlineKeyboardButton("⚙️ Settings", callback_data=CB_SETTINGS),
        ],
        [
            InlineKeyboardButton("🆘 Help", callback_data=CB_HELP),
            InlineKeyboardButton("🔄 Refresh", callback_data=CB_REFRESH),
        ],
    ])

def wallets_screen_text(wallet_rows: List[Tuple[int, str, str, int, Optional[int]]], page: int, per_page: int = 6) -> str:
    total = len(wallet_rows)
    if total == 0:
        return (
            "👛 *My Wallets*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "You have *0* wallets saved.\n\n"
            "Tap ⚡ *Add Wallet* to get started."
        )

    pages = (total + per_page - 1) // per_page
    page = max(0, min(page, pages - 1))

    start = page * per_page
    chunk = wallet_rows[start:start + per_page]

    lines = [
        f"👛 *My Wallets*  (Page {page+1}/{pages})",
        "━━━━━━━━━━━━━━━━━━━━",
        ""
    ]

    for wid, name, addr, enabled, last_lamports in chunk:
        status = "🔔 ON" if enabled == 1 else "🔕 OFF"
        last_sol = "—"
        if last_lamports is not None:
            last_sol = f"{lamports_to_sol(last_lamports):.9f}".rstrip("0").rstrip(".")
        lines.append(
            f"• *{name}*  {status}\n"
            f"  `{truncate_addr(addr)}`\n"
            f"  💰 last: *{last_sol}* SOL\n"
        )

    lines.append("Tip: Toggle alerts per wallet or remove it 👇")
    return "\n".join(lines)

def wallets_screen_keyboard(wallet_rows: List[Tuple[int, str, str, int, Optional[int]]], page: int, per_page: int = 6) -> InlineKeyboardMarkup:
    total = len(wallet_rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))

    start = page * per_page
    chunk = wallet_rows[start:start + per_page]

    buttons = []

    for wid, name, _addr, _enabled, _last in chunk:
        buttons.append([
            InlineKeyboardButton(f"🔔 Toggle — {name}", callback_data=f"{CB_TOGGLE_PREFIX}{wid}"),
            InlineKeyboardButton(f"🗑 Remove — {name}", callback_data=f"{CB_DELETE_PREFIX}{wid}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{CB_WALLETS_PAGE_PREFIX}{page-1}"))
    nav.append(InlineKeyboardButton("➕ Add", callback_data=CB_ADD))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{CB_WALLETS_PAGE_PREFIX}{page+1}"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton("🏠 Back to Dashboard", callback_data=CB_BACK_MENU)])
    return InlineKeyboardMarkup(buttons)

def alerts_screen_text(wallet_rows: List[Tuple[int, str, str, int, Optional[int]]]) -> str:
    total = len(wallet_rows)
    enabled = sum(1 for _id, _n, _a, en, _l in wallet_rows if en == 1)
    disabled = total - enabled

    return (
        "🔔 *Alerts*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "This bot sends a message when a wallet balance changes by *any* SOL amount.\n\n"
        f"✅ Alerts ON: *{enabled}*\n"
        f"🚫 Alerts OFF: *{disabled}*\n"
        f"👛 Total wallets: *{total}*\n\n"
        "Manage toggles under 👛 *My Wallets*."
    )

def settings_screen_text() -> str:
    return (
        "⚙️ *Settings*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Currently available:\n"
        f"⏱ Scan interval: *{CHECK_INTERVAL_SECONDS}s*\n\n"
        "Coming soon (if you want):\n"
        "• Minimum change threshold per wallet\n"
        "• Silent hours (do not disturb)\n"
        "• Rename wallet\n"
        "• Alert history\n"
    )

def help_screen_text() -> str:
    return (
        "🆘 *Help*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ *How to use*\n"
        "1) Tap ⚡ Add Wallet\n"
        "2) Send a wallet name\n"
        "3) Send a Solana wallet address\n"
        "4) You’ll get alerts when balance changes\n\n"
        "🧠 *Commands*\n"
        "/start — open dashboard\n"
        "/cancel — cancel add-wallet flow\n\n"
        "If you want this to scale to lots of users, ask me to switch it to Helius WebSockets."
    )

def simple_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Dashboard", callback_data=CB_BACK_MENU)]])

# =========================
# Screen rendering
# =========================
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    user_id = update.effective_user.id
    rows = db_get_wallets(user_id)

    last_check = context.application.bot_data.get("last_check_iso")
    text = dashboard_text(rows, last_check)
    kb = main_menu_keyboard_modern()

    if update.callback_query:
        if edit:
            await update.callback_query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        else:
            await update.callback_query.message.reply_text(text=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text=text, reply_markup=kb, parse_mode="Markdown")

# =========================
# Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)

async def menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id

    # Dashboard actions
    if data == CB_REFRESH:
        await show_main_menu(update, context, edit=True)
        return ConversationHandler.END

    if data == CB_BACK_MENU:
        await show_main_menu(update, context, edit=True)
        return ConversationHandler.END

    if data == CB_ADD:
        await q.edit_message_text(
            "⚡ *Add Wallet*\n\nSend the *wallet name* (example: `Main Wallet`).",
            parse_mode="Markdown"
        )
        return ASK_NAME

    # Screens
    if data == CB_WALLETS:
        rows = db_get_wallets(user_id)
        await q.edit_message_text(
            text=wallets_screen_text(rows, page=0),
            reply_markup=wallets_screen_keyboard(rows, page=0),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if data.startswith(CB_WALLETS_PAGE_PREFIX):
        page = int(data.split(":", 1)[1])
        rows = db_get_wallets(user_id)
        await q.edit_message_text(
            text=wallets_screen_text(rows, page=page),
            reply_markup=wallets_screen_keyboard(rows, page=page),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if data == CB_ALERTS:
        rows = db_get_wallets(user_id)
        await q.edit_message_text(
            text=alerts_screen_text(rows),
            reply_markup=simple_back_keyboard(),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if data == CB_SETTINGS:
        await q.edit_message_text(
            text=settings_screen_text(),
            reply_markup=simple_back_keyboard(),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if data == CB_HELP:
        await q.edit_message_text(
            text=help_screen_text(),
            reply_markup=simple_back_keyboard(),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Wallet actions
    if data.startswith(CB_TOGGLE_PREFIX):
        wallet_id = int(data.split(":", 1)[1])
        new_enabled = db_toggle_wallet(user_id, wallet_id)
        if new_enabled is None:
            await q.message.reply_text("⚠️ Couldn’t find that wallet.")
        else:
            await q.message.reply_text("✅ Notifications enabled 🔔" if new_enabled == 1 else "✅ Notifications disabled 🔕")

        # Return user to wallets screen page 0
        rows = db_get_wallets(user_id)
        await q.edit_message_text(
            text=wallets_screen_text(rows, page=0),
            reply_markup=wallets_screen_keyboard(rows, page=0),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if data.startswith(CB_DELETE_PREFIX):
        wallet_id = int(data.split(":", 1)[1])
        ok = db_delete_wallet(user_id, wallet_id)
        await q.message.reply_text("🗑 Removed ✅" if ok else "⚠️ Couldn’t remove (not found).")

        rows = db_get_wallets(user_id)
        await q.edit_message_text(
            text=wallets_screen_text(rows, page=0),
            reply_markup=wallets_screen_keyboard(rows, page=0),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    return ConversationHandler.END

async def add_wallet_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if len(name) < 1 or len(name) > 40:
        await update.message.reply_text("⚠️ Name must be 1–40 characters. Send the wallet name again.")
        return ASK_NAME

    context.user_data["new_wallet_name"] = name
    await update.message.reply_text(
        "📩 Now send the *wallet address* (Solana base58).",
        parse_mode="Markdown",
    )
    return ASK_ADDRESS

async def add_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    addr = (update.message.text or "").strip()

    if not is_probably_solana_address(addr):
        await update.message.reply_text("⚠️ That doesn’t look like a valid Solana address. Send the address again.")
        return ASK_ADDRESS

    name = context.user_data.get("new_wallet_name", "Wallet")
    ok, msg = db_add_wallet(update.effective_user.id, name, addr)
    await update.message.reply_text(msg)

    # Set initial last balance so it doesn't "spam" on first scan
    session: aiohttp.ClientSession = context.application.bot_data["http"]
    try:
        lamports = await rpc_get_balance_lamports(session, addr)
        # Find matching wallet row and store last_lamports
        for wid, _n, a, _en, _last in db_get_wallets(update.effective_user.id):
            if a == addr:
                db_update_last_lamports(wid, lamports)
                break
    except Exception as e:
        logger.warning("Initial balance fetch failed: %s", e)

    context.user_data.pop("new_wallet_name", None)
    await show_main_menu(update, context)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_wallet_name", None)
    await update.message.reply_text("✅ Cancelled.")
    await show_main_menu(update, context)
    return ConversationHandler.END

# =========================
# Background balance monitor
# =========================
async def check_balances_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app: Application = context.application
    session: aiohttp.ClientSession = app.bot_data["http"]

    # store dashboard timestamp
    app.bot_data["last_check_iso"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = db_get_enabled_wallets_all_users()
    if not rows:
        return

    sem = asyncio.Semaphore(8)

    async def process_one(wallet_id: int, user_id: int, name: str, address: str, last_lamports: Optional[int]):
        async with sem:
            try:
                current = await rpc_get_balance_lamports(session, address)
            except Exception as e:
                logger.warning("Balance fetch failed for %s: %s", address, e)
                return

            if last_lamports is None:
                db_update_last_lamports(wallet_id, current)
                return

            if current != last_lamports:
                delta = current - last_lamports
                db_update_last_lamports(wallet_id, current)

                delta_sol = lamports_to_sol(abs(delta))
                current_sol = lamports_to_sol(current)

                direction = "increased 📈" if delta > 0 else "decreased 📉"
                sign = "+" if delta > 0 else "-"

                msg = (
                    f"💰 *Balance Update*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"👛 *{name}*\n"
                    f"`{truncate_addr(address)}`\n\n"
                    f"{direction}\n"
                    f"Change: *{sign}{delta_sol:.9f}* SOL\n"
                    f"New balance: *{current_sol:.9f}* SOL"
                )

                try:
                    await app.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.warning("Failed to notify user %s: %s", user_id, e)

    await asyncio.gather(*(process_one(*r) for r in rows), return_exceptions=True)

# =========================
# Startup / Shutdown
# =========================
async def on_startup(app: Application) -> None:
    db_init()
    app.bot_data["http"] = aiohttp.ClientSession()
    app.bot_data["last_check_iso"] = None

    app.job_queue.run_repeating(check_balances_job, interval=CHECK_INTERVAL_SECONDS, first=5)
    logger.info("Bot started. Checking every %ss", CHECK_INTERVAL_SECONDS)

async def on_shutdown(app: Application) -> None:
    session: aiohttp.ClientSession = app.bot_data.get("http")
    if session:
        await session.close()
    logger.info("Bot stopped.")

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN env var.")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_click)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_name)],
            ASK_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_address)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start_cmd),
            CallbackQueryHandler(menu_click),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(conv)
    application.add_handler(CallbackQueryHandler(menu_click))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()