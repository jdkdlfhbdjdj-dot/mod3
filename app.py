import logging
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from flask import Flask, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    import stripe
except ImportError:
    stripe = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("stonedigger")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Stonedigger_bot")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STARS_PRICE = int(os.environ.get("STARS_PRICE", "100"))

app = Flask(__name__)
DB_PATH = os.environ.get("SQLITE_PATH", "/tmp/stonedigger.db")


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            referrer_code TEXT,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            name TEXT NOT NULL,
            affiliate_url TEXT NOT NULL,
            commission_percent REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
        );
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            click_id TEXT UNIQUE NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            campaign_code TEXT,
            offer_id INTEGER,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(offer_id) REFERENCES offers(id)
        );
        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            click_id TEXT,
            telegram_user_id INTEGER,
            provider TEXT NOT NULL,
            external_id TEXT UNIQUE NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
            commission REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            FOREIGN KEY(click_id) REFERENCES clicks(click_id)
        );
        CREATE TABLE IF NOT EXISTS stars_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER NOT NULL,
            payload TEXT UNIQUE NOT NULL,
            charge_id TEXT,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT OR IGNORE INTO campaigns(code,name) VALUES(?,?)", ("default", "Default Campaign"))
    conn.commit()
    conn.close()


def now():
    return int(time.time())


def save_user(tg_user, referrer=None):
    conn = db()
    existing = conn.execute("SELECT referrer_code FROM users WHERE telegram_user_id=?", (tg_user.id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO users(telegram_user_id,username,first_name,referrer_code,created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (tg_user.id, tg_user.username, tg_user.first_name, referrer, now(), now()),
        )
    else:
        conn.execute(
            "UPDATE users SET username=?, first_name=?, last_seen_at=? WHERE telegram_user_id=?",
            (tg_user.username, tg_user.first_name, now(), tg_user.id),
        )
    conn.commit()
    conn.close()


def create_click(user_id, campaign_code="default", offer_id=None):
    click_id = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    conn = db()
    conn.execute(
        "INSERT INTO clicks(click_id,telegram_user_id,campaign_code,offer_id,created_at) VALUES(?,?,?,?,?)",
        (click_id, user_id, campaign_code, offer_id, now()),
    )
    conn.commit()
    conn.close()
    return click_id


def get_stats():
    conn = db()
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM users) users,(SELECT COUNT(*) FROM clicks) clicks,(SELECT COUNT(*) FROM conversions WHERE status='confirmed') conversions,(SELECT COALESCE(SUM(commission),0) FROM conversions WHERE status='confirmed') commission"
    ).fetchone()
    conn.close()
    return dict(row)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    ref = args[0] if args else None
    save_user(user, referrer=ref)
    click_id = create_click(user.id, campaign_code=ref or "default")

    keyboard = [
        [InlineKeyboardButton("🚀 Get Started", callback_data="noop")],
        [InlineKeyboardButton("💎 Premium with Stars", callback_data="stars")],
    ]
    text = (
        f"Welcome to Stone, {user.first_name or 'there'}!\n\n"
        "Your account is registered and your referral source has been tracked.\n\n"
        f"Click ID: `{click_id}`\n\n"
        "Choose an option below."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — register and track referral\n"
        "/link — generate your referral link\n"
        "/stats — bot statistics (admin only)\n"
        "/stars — buy Premium with Telegram Stars"
    )


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)
    code = f"u{user.id}"
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{code}"
    await update.message.reply_text(f"Your referral link:\n{link}\n\nShare it to track referrals.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_ids = {x.strip() for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
    if str(update.effective_user.id) not in admin_ids:
        await update.message.reply_text("Admin access only.")
        return
    s = get_stats()
    await update.message.reply_text(
        f"Users: {s['users']}\nClicks: {s['clicks']}\nConfirmed conversions: {s['conversions']}\nCommission: {s['commission']:.2f}"
    )


async def stars_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = f"premium:{update.effective_user.id}:{secrets.token_hex(8)}"
    await update.message.reply_invoice(
        title="Stone Premium",
        description="Premium digital access inside Stone.",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice("Premium", STARS_PRICE)],
    )


def stripe_checkout(click_id, user_id):
    if not stripe or not STRIPE_SECRET_KEY:
        return None
    stripe.api_key = STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": os.environ["STRIPE_PRICE_ID"], "quantity": 1}],
        success_url=f"{PUBLIC_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_BASE_URL}/cancel",
        client_reference_id=str(user_id),
        metadata={"click_id": click_id, "telegram_user_id": str(user_id)},
    )
    return session.url


@app.get("/")
def home():
    return jsonify({"service": "StoneDigger", "status": "ok", "stats": get_stats()})


@app.get("/health")
def health():
    return jsonify({"status": "healthy"})


@app.get("/success")
def success():
    return "Payment received. You can return to Telegram."


@app.get("/cancel")
def cancel():
    return "Payment cancelled."


@app.get("/buy/<click_id>")
def buy(click_id):
    conn = db()
    click = conn.execute("SELECT * FROM clicks WHERE click_id=?", (click_id,)).fetchone()
    conn.close()
    if not click:
        return jsonify({"error": "invalid_click_id"}), 404
    url = stripe_checkout(click_id, click["telegram_user_id"])
    if not url:
        return jsonify({"error": "stripe_not_configured", "click_id": click_id}), 503
    return jsonify({"checkout_url": url})


@app.post("/stripe/webhook")
def stripe_webhook():
    if not stripe or not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "stripe_webhook_not_configured"}), 503
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as exc:
        log.warning("Stripe webhook rejected: %s", exc)
        return jsonify({"error": "invalid_signature"}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata", {})
        click_id = metadata.get("click_id")
        user_id = metadata.get("telegram_user_id")
        external_id = session.get("id")
        amount = (session.get("amount_total") or 0) / 100
        conn = db()
        click = conn.execute("SELECT offer_id FROM clicks WHERE click_id=?", (click_id,)).fetchone()
        commission = 0
        if click and click["offer_id"]:
            offer = conn.execute("SELECT commission_percent FROM offers WHERE id=?", (click["offer_id"],)).fetchone()
            if offer:
                commission = amount * float(offer["commission_percent"]) / 100
        conn.execute(
            "INSERT OR IGNORE INTO conversions(click_id,telegram_user_id,provider,external_id,amount,currency,commission,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (click_id, int(user_id) if user_id else None, "stripe", external_id, amount, session.get("currency", "usd").upper(), commission, "confirmed", now()),
        )
        conn.commit()
        conn.close()
    return jsonify({"received": True})


@app.post("/telegram/webhook")
def telegram_webhook():
    return jsonify({"ok": True})


def run_http():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("link", link_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("stars", stars_cmd))

    threading.Thread(target=run_http, daemon=True).start()
    log.info("StoneDigger starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
