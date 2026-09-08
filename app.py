import logging
import os
import secrets
import threading
import time

from flask import Flask, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

try:
    import stripe
except ImportError:
    stripe = None

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

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


def db():
    if not psycopg:
        raise RuntimeError("psycopg is required")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.users') AS users_table")
            row = cur.fetchone()
            if not row or row["users_table"] != "users":
                raise RuntimeError("Supabase schema is missing public.users")
    finally:
        conn.close()


def save_user(tg_user, referrer=None):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT referrer_code FROM public.users WHERE telegram_user_id=%s", (tg_user.id,))
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO public.users(telegram_user_id,username,first_name,referrer_code) VALUES(%s,%s,%s,%s)",
                    (tg_user.id, tg_user.username, tg_user.first_name, referrer),
                )
            else:
                cur.execute(
                    "UPDATE public.users SET username=%s, first_name=%s, last_seen_at=now() WHERE telegram_user_id=%s",
                    (tg_user.username, tg_user.first_name, tg_user.id),
                )
        conn.commit()
    finally:
        conn.close()


def get_oxshare_offer():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, affiliate_url FROM public.offers "
                "WHERE lower(name)='oxshare' AND active=true ORDER BY id LIMIT 1"
            )
            return cur.fetchone()
    finally:
        conn.close()


def create_click(user_id, campaign_code="default", offer_id=None):
    if offer_id is None:
        offer = get_oxshare_offer()
        offer_id = offer["id"] if offer else None
    click_id = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.clicks(click_id,telegram_user_id,campaign_code,offer_id) VALUES(%s,%s,%s,%s)",
                (click_id, user_id, campaign_code, offer_id),
            )
        conn.commit()
    finally:
        conn.close()
    return click_id


def get_stats():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM public.users) AS users,
                  (SELECT COUNT(*) FROM public.clicks) AS clicks,
                  (SELECT COUNT(*) FROM public.conversions WHERE status IN ('approved','paid')) AS conversions,
                  (SELECT COALESCE(SUM(commission),0) FROM public.conversions WHERE status IN ('approved','paid')) AS commission
                """
            )
            return dict(cur.fetchone())
    finally:
        conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref = context.args[0] if context.args else None
    save_user(user, referrer=ref)
    click_id = create_click(user.id, campaign_code=ref or "default")
    offer = get_oxshare_offer()

    keyboard = []
    if offer:
        keyboard.append([InlineKeyboardButton("🚀 Open Oxshare", url=offer["affiliate_url"])])
    keyboard.append([InlineKeyboardButton("💎 Premium with Stars", callback_data="stars")])

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
        f"Users: {s['users']}\nClicks: {s['clicks']}\nConversions: {s['conversions']}\nCommission: {float(s['commission'] or 0):.2f}"
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


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "stars":
        await stars_cmd(update, context)


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
    try:
        conn = db()
        conn.close()
        return jsonify({"status": "healthy", "database": "ok"})
    except Exception as exc:
        log.exception("Database health check failed")
        return jsonify({"status": "unhealthy", "database": "error", "error": str(exc)}), 503


@app.get("/success")
def success():
    return "Payment received. You can return to Telegram."


@app.get("/cancel")
def cancel():
    return "Payment cancelled."


@app.get("/buy/<click_id>")
def buy(click_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.clicks WHERE click_id=%s", (click_id,))
            click = cur.fetchone()
    finally:
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
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT offer_id FROM public.clicks WHERE click_id=%s", (click_id,))
                click = cur.fetchone()
                commission = 0
                if click and click["offer_id"]:
                    cur.execute("SELECT commission_percent FROM public.offers WHERE id=%s", (click["offer_id"],))
                    offer = cur.fetchone()
                    if offer:
                        commission = amount * float(offer["commission_percent"]) / 100
                cur.execute(
                    """
                    INSERT INTO public.conversions
                    (click_id,telegram_user_id,provider,external_id,amount,currency,commission,status)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (external_id) DO NOTHING
                    """,
                    (click_id, int(user_id) if user_id else None, "stripe", external_id, amount,
                     session.get("currency", "usd").upper(), commission, "approved"),
                )
            conn.commit()
        finally:
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
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("link", link_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("stars", stars_cmd))
    application.add_handler(CallbackQueryHandler(button_callback))

    threading.Thread(target=run_http, daemon=True).start()
    log.info("StoneDigger starting with Supabase PostgreSQL and Oxshare only")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
