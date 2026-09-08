import csv
import html
import logging
import os
import secrets
import threading
from datetime import date, datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

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
ADMIN_DASHBOARD_USER = os.environ.get("ADMIN_DASHBOARD_USER", "")
ADMIN_DASHBOARD_PASSWORD = os.environ.get("ADMIN_DASHBOARD_PASSWORD", "")

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


def parse_start(raw):
    referral = raw
    campaign = "default"
    if raw and "__c_" in raw:
        referral, campaign = raw.split("__c_", 1)
        campaign = campaign[:80] or "default"
    return referral, campaign


def parse_referrer(ref):
    if not ref or not ref.startswith("ref_u"):
        return None
    try:
        return int(ref[5:])
    except (TypeError, ValueError):
        return None


def save_user(tg_user, referrer=None):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_user_id FROM public.users WHERE telegram_user_id=%s", (tg_user.id,))
            existing = cur.fetchone()
            if existing is None:
                referrer_id = parse_referrer(referrer)
                if referrer_id == tg_user.id:
                    referrer_id = None
                if referrer_id:
                    cur.execute("SELECT 1 FROM public.users WHERE telegram_user_id=%s", (referrer_id,))
                    if not cur.fetchone():
                        referrer_id = None
                cur.execute(
                    """INSERT INTO public.users
                    (telegram_user_id, username, first_name, referrer_code, referred_by_telegram_user_id)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (tg_user.id, tg_user.username, tg_user.first_name, referrer, referrer_id),
                )
            else:
                cur.execute(
                    """UPDATE public.users SET username=%s, first_name=%s, last_seen_at=now()
                    WHERE telegram_user_id=%s""",
                    (tg_user.username, tg_user.first_name, tg_user.id),
                )
        conn.commit()
    finally:
        conn.close()


def get_user_stats(user_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT activity_count, streak, last_dig_date,
                (SELECT COUNT(*) FROM public.users r WHERE r.referred_by_telegram_user_id=u.telegram_user_id) AS referrals
                FROM public.users u WHERE telegram_user_id=%s""", (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def record_dig(user_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT activity_count, streak, last_dig_date FROM public.users WHERE telegram_user_id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if row is None:
                return None
            today = date.today()
            last_dig = row["last_dig_date"]
            if last_dig == today:
                return {"dug": False, "activity": row["activity_count"] or 0, "streak": row["streak"] or 0}
            new_streak = (row["streak"] or 0) + 1 if last_dig == today - timedelta(days=1) else 1
            new_activity = (row["activity_count"] or 0) + 1
            cur.execute("""UPDATE public.users SET activity_count=%s, streak=%s, last_dig_date=%s, last_seen_at=now()
                           WHERE telegram_user_id=%s""", (new_activity, new_streak, today, user_id))
        conn.commit()
        return {"dug": True, "activity": new_activity, "streak": new_streak}
    finally:
        conn.close()


def get_oxshare_offer():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,name,affiliate_url FROM public.offers
                           WHERE lower(name)='oxshare' AND active=true ORDER BY id LIMIT 1""")
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
            cur.execute("INSERT INTO public.clicks(click_id,telegram_user_id,campaign_code,offer_id) VALUES(%s,%s,%s,%s)",
                        (click_id, user_id, campaign_code, offer_id))
        conn.commit()
    finally:
        conn.close()
    return click_id


def leaderboard(limit=10):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(username,first_name,'Miner') AS display_name,activity_count,streak
                           FROM public.users ORDER BY activity_count DESC,streak DESC,created_at ASC LIMIT %s""", (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def get_stats():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT (SELECT COUNT(*) FROM public.users) AS users,
            (SELECT COUNT(*) FROM public.clicks) AS clicks,
            (SELECT COUNT(*) FROM public.users WHERE referred_by_telegram_user_id IS NOT NULL) AS referrals,
            (SELECT COUNT(*) FROM public.conversions WHERE status IN ('approved','paid')) AS conversions,
            (SELECT COALESCE(SUM(amount),0) FROM public.conversions WHERE status IN ('approved','paid')) AS revenue,
            (SELECT COALESCE(SUM(commission),0) FROM public.conversions WHERE status IN ('approved','paid')) AS commission""")
            return dict(cur.fetchone())
    finally:
        conn.close()


def main_keyboard(offer):
    keyboard = [[InlineKeyboardButton("⛏️ DIG NOW", callback_data="dig")],
                 [InlineKeyboardButton("🏆 LEADERBOARD", callback_data="leaderboard")]]
    if offer:
        keyboard.append([InlineKeyboardButton("🚀 OPEN OXSHARE", url=offer["affiliate_url"])])
    keyboard.extend([[InlineKeyboardButton("🔗 MY REFERRAL LINK", callback_data="link")],
                     [InlineKeyboardButton("💎 PREMIUM WITH STARS", callback_data="stars")]])
    return InlineKeyboardMarkup(keyboard)


def welcome_text(user, stats):
    return ("⛏️ *WELCOME TO STONEDIGGER* ⛏️\n\n"
            f"*{user.first_name or 'Miner'}*\nFREE\n\n"
            f"📊 Activity: *{stats['activity_count'] or 0}*    🔥 Streak: *{stats['streak'] or 0}*    👥 Referrals: *{stats['referrals'] or 0}*\n\n"
            "🎯 *TAP DIG NOW TO PLAY.*\n\n💰 *EXPLORE THE SEPARATE OXSHARE OPPORTUNITY BELOW.*")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    raw = context.args[0] if context.args else None
    ref, campaign = parse_start(raw)
    save_user(user, referrer=ref)
    offer = get_oxshare_offer()
    if offer:
        create_click(user.id, campaign_code=campaign, offer_id=offer["id"])
    stats = get_user_stats(user.id)
    await update.message.reply_text(welcome_text(user, stats), parse_mode="Markdown", reply_markup=main_keyboard(offer))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start — open StoneDigger\n/link — get your referral link\n/stats — admin statistics\n/stars — Premium with Telegram Stars")


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_u{user.id}"
    await update.message.reply_text(f"🔗 Your StoneDigger referral link:\n{link}\n\nShare it and track your referrals.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_ids = {x.strip() for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
    if str(update.effective_user.id) not in admin_ids:
        await update.message.reply_text("Admin access only.")
        return
    s = get_stats()
    await update.message.reply_text(f"Users: {s['users']}\nClicks: {s['clicks']}\nReferrals: {s['referrals']}\nConversions: {s['conversions']}\nRevenue: {float(s['revenue'] or 0):.2f}\nCommission: {float(s['commission'] or 0):.2f}")


async def stars_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = f"premium:{update.effective_user.id}:{secrets.token_hex(8)}"
    await update.message.reply_invoice(title="Stone Premium", description="Premium digital access inside StoneDigger.", payload=payload, currency="XTR", prices=[LabeledPrice("Premium", STARS_PRICE)])


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "dig":
        result = record_dig(query.from_user.id)
        if not result:
            await query.message.reply_text("Please send /start first.")
            return
        if not result["dug"]:
            await query.message.reply_text(f"⏳ *YOU ALREADY DUG TODAY!*\n\n📊 Activity: *{result['activity']}*    🔥 Streak: *{result['streak']}*\n\nCome back tomorrow to keep your streak alive.", parse_mode="Markdown")
            return
        await query.message.reply_text(f"⛏️ *DIG COMPLETE!*\n\n📊 Activity: *{result['activity']}*    🔥 Streak: *{result['streak']}*\n\nKeep your streak alive tomorrow.\n\n💰 Check the separate Oxshare opportunity below.", parse_mode="Markdown", reply_markup=main_keyboard(get_oxshare_offer()))
    elif query.data == "leaderboard":
        rows = leaderboard()
        lines = ["🏆 *STONEDIGGER LEADERBOARD*", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {str(row['display_name'] or 'Miner')[:24]} — ⛏️{row['activity_count'] or 0} • 🔥{row['streak'] or 0}")
        if len(lines) == 2:
            lines.append("No miners yet.")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard(get_oxshare_offer()))
    elif query.data == "link":
        await query.message.reply_text(f"🔗 Your StoneDigger referral link:\nhttps://t.me/{BOT_USERNAME}?start=ref_u{query.from_user.id}\n\nShare it to grow your referrals.")
    elif query.data == "stars":
        await stars_cmd(update, context)


def stripe_checkout(click_id, user_id):
    if not stripe or not STRIPE_SECRET_KEY:
        return None
    stripe.api_key = STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(mode="payment", line_items=[{"price": os.environ["STRIPE_PRICE_ID"], "quantity": 1}],
        success_url=f"{PUBLIC_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}", cancel_url=f"{PUBLIC_BASE_URL}/cancel",
        client_reference_id=str(user_id), metadata={"click_id": click_id, "telegram_user_id": str(user_id)})
    return session.url


def dashboard_auth():
    auth = request.authorization
    if not ADMIN_DASHBOARD_USER or not ADMIN_DASHBOARD_PASSWORD or not auth or auth.username != ADMIN_DASHBOARD_USER or auth.password != ADMIN_DASHBOARD_PASSWORD:
        return Response("Admin login required.", 401, {"WWW-Authenticate": 'Basic realm="StoneDigger Admin"'})
    return None


@app.get("/")
def home():
    return jsonify({"service": "StoneDigger", "status": "ok", "stats": get_stats()})


@app.get("/health")
def health():
    try:
        conn = db(); conn.close()
        return jsonify({"status": "healthy", "database": "ok"})
    except Exception as exc:
        log.exception("Database health check failed")
        return jsonify({"status": "unhealthy", "database": "error", "error": str(exc)}), 503


@app.get("/dashboard")
def dashboard():
    auth_error = dashboard_auth()
    if auth_error:
        return auth_error
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT (SELECT COUNT(*) FROM public.users) users,
            (SELECT COUNT(*) FROM public.clicks) clicks,
            (SELECT COUNT(*) FROM public.users WHERE referred_by_telegram_user_id IS NOT NULL) referrals,
            (SELECT COUNT(*) FROM public.conversions WHERE status IN ('approved','paid')) conversions,
            (SELECT COALESCE(SUM(amount),0) FROM public.conversions WHERE status IN ('approved','paid')) revenue,
            (SELECT COALESCE(SUM(commission),0) FROM public.conversions WHERE status IN ('approved','paid')) commission""")
            k = cur.fetchone()
            cur.execute("""SELECT COALESCE(u.username,u.first_name,'Miner') name,COUNT(r.telegram_user_id) referrals,COALESCE(u.activity_count,0) activity,COALESCE(u.streak,0) streak
                           FROM public.users u LEFT JOIN public.users r ON r.referred_by_telegram_user_id=u.telegram_user_id
                           GROUP BY u.telegram_user_id,u.username,u.first_name,u.activity_count,u.streak ORDER BY referrals DESC,activity DESC LIMIT 20""")
            top = cur.fetchall()
            cur.execute("SELECT COALESCE(username,first_name,'Miner') name,telegram_user_id,activity_count,streak,referred_by_telegram_user_id,created_at FROM public.users ORDER BY created_at DESC LIMIT 30")
            users = cur.fetchall()
            cur.execute("SELECT c.click_id,c.telegram_user_id,c.campaign_code,COALESCE(o.name,'—') offer,c.created_at FROM public.clicks c LEFT JOIN public.offers o ON o.id=c.offer_id ORDER BY c.created_at DESC LIMIT 30")
            clicks = cur.fetchall()
            cur.execute("SELECT click_id,telegram_user_id,provider,amount,currency,commission,status,created_at FROM public.conversions ORDER BY created_at DESC LIMIT 30")
            conversions = cur.fetchall()
            cur.execute("SELECT name,affiliate_url,active FROM public.offers WHERE lower(name)='oxshare' ORDER BY id LIMIT 1")
            offer = cur.fetchone()
            cur.execute("""SELECT campaign_code,COUNT(*) clicks,COUNT(DISTINCT telegram_user_id) users
                           FROM public.clicks GROUP BY campaign_code ORDER BY clicks DESC LIMIT 30""")
            campaigns = cur.fetchall()
            cur.execute("""SELECT date_trunc('day',created_at)::date day,COUNT(*) clicks,COUNT(DISTINCT telegram_user_id) users
                           FROM public.clicks WHERE created_at >= now()-interval '30 days' GROUP BY 1 ORDER BY 1""")
            daily = cur.fetchall()
    finally:
        conn.close()

    def e(v): return html.escape(str(v if v is not None else ""))
    def n(v): return int(v or 0)
    def money(v):
        try: return f"{float(v or 0):,.2f}"
        except Exception: return "0.00"
    cards = f"""
    <div class='grid'>
    <div class='card'><small>USERS</small><b>{n(k['users'])}</b></div><div class='card'><small>CLICKS</small><b>{n(k['clicks'])}</b></div>
    <div class='card'><small>REFERRALS</small><b>{n(k['referrals'])}</b></div><div class='card'><small>CONVERSIONS</small><b>{n(k['conversions'])}</b></div>
    <div class='card'><small>REVENUE</small><b>{money(k['revenue'])}</b></div><div class='card'><small>COMMISSION</small><b>{money(k['commission'])}</b></div>
    </div>"""
    top_rows=''.join(f"<tr><td>{i}</td><td>{e(r['name'])}</td><td>{n(r['referrals'])}</td><td>{n(r['activity'])}</td><td>{n(r['streak'])}</td></tr>" for i,r in enumerate(top,1))
    campaign_rows=''.join(f"<tr><td><b>{e(r['campaign_code'])}</b></td><td>{n(r['clicks'])}</td><td>{n(r['users'])}</td></tr>" for r in campaigns)
    user_rows=''.join(f"<tr><td>{e(r['name'])}</td><td>{r['telegram_user_id']}</td><td>{n(r['activity_count'])}</td><td>{n(r['streak'])}</td><td>{'Yes' if r['referred_by_telegram_user_id'] else 'No'}</td></tr>" for r in users)
    click_rows=''.join(f"<tr><td>{e(r['click_id'])}</td><td>{r['telegram_user_id']}</td><td>{e(r['campaign_code'])}</td><td>{e(r['offer'])}</td></tr>" for r in clicks)
    conversion_rows=''.join(f"<tr><td>{e(r['click_id'])}</td><td>{r['telegram_user_id']}</td><td>{e(r['provider'])}</td><td>{money(r['amount'])} {e(r['currency'])}</td><td>{money(r['commission'])}</td><td>{e(r['status'])}</td></tr>" for r in conversions)
    daily_json = '['+','.join('{"day":"'+e(r['day'])+'","clicks":'+str(n(r['clicks']))+',"users":'+str(n(r['users']))+'}' for r in daily)+']'
    offer_html = f"<div class='offer'>OXSHARE: {e(offer['affiliate_url']) if offer else 'not configured'}</div>"
    page=f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>StoneDigger Command Center</title>
    <style>body{{margin:0;background:#0b1020;color:#eef2ff;font:14px system-ui}}.wrap{{max-width:1250px;margin:auto;padding:25px}}h1{{margin:0 0 5px}}.muted,small{{color:#93a0bb}}.grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:20px 0}}.card,.section{{background:#151d33;border:1px solid #293552;border-radius:15px;padding:16px;margin-bottom:15px}}.card b{{display:block;font-size:27px;margin-top:6px}}.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border-bottom:1px solid #293552;text-align:left;white-space:nowrap}}th{{color:#aeb9d1;font-size:11px}}.offer{{padding:14px;background:#10251c;border:1px solid #285b43;border-radius:12px;word-break:break-all}}canvas{{width:100%;height:220px}}@media(max-width:1000px){{.grid{{grid-template-columns:repeat(3,1fr)}}}}@media(max-width:600px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}</style></head>
    <body><div class='wrap'><h1>⛏️ StoneDigger Marketing Command Center</h1><div class='muted'>Oxshare tracking • Facebook • Instagram • Telegram</div>{offer_html}{cards}
    <div class='section'><h2>📈 30-Day Traffic</h2><canvas id='chart' width='1200' height='220'></canvas></div>
    <div class='section'><h2>📣 Campaign Performance</h2><div class='scroll'><table><tr><th>Campaign</th><th>Clicks</th><th>Unique Users</th></tr>{campaign_rows or '<tr><td colspan=3>No campaign data</td></tr>'}</table></div></div>
    <div class='section'><h2>🏆 Top Referrers</h2><div class='scroll'><table><tr><th>#</th><th>User</th><th>Referrals</th><th>Activity</th><th>Streak</th></tr>{top_rows}</table></div></div>
    <div class='section'><h2>👥 Users</h2><div class='scroll'><table><tr><th>User</th><th>Telegram ID</th><th>Activity</th><th>Streak</th><th>Referred</th></tr>{user_rows}</table></div></div>
    <div class='section'><h2>🔗 Clicks</h2><div class='scroll'><table><tr><th>Click ID</th><th>User</th><th>Campaign</th><th>Offer</th></tr>{click_rows}</table></div></div>
    <div class='section'><h2>💰 Conversions</h2><div class='scroll'><table><tr><th>Click ID</th><th>User</th><th>Provider</th><th>Amount</th><th>Commission</th><th>Status</th></tr>{conversion_rows}</table></div></div>
    <script>const d={daily_json};const c=document.getElementById('chart'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);if(d.length){{const m=Math.max(1,...d.map(v=>v.clicks)),w=c.width/d.length;d.forEach((v,i)=>{{const h=(v.clicks/m)*170;x.fillRect(i*w+2,190-h,Math.max(3,w-4),h);}});}}</script></div></body></html>"""
    return Response(page,mimetype='text/html')


@app.get("/dashboard/export")
def dashboard_export():
    auth_error=dashboard_auth()
    if auth_error: return auth_error
    conn=db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_user_id,username,first_name,referrer_code,referred_by_telegram_user_id,activity_count,streak,created_at,last_seen_at FROM public.users ORDER BY created_at DESC")
            rows=cur.fetchall()
    finally: conn.close()
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(["telegram_user_id","username","first_name","referrer_code","referred_by_telegram_user_id","activity_count","streak","created_at","last_seen_at"])
    for r in rows: writer.writerow([r.get(k) for k in ["telegram_user_id","username","first_name","referrer_code","referred_by_telegram_user_id","activity_count","streak","created_at","last_seen_at"]])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=stonedigger_users.csv'})


@app.get("/success")
def success(): return "Payment received. You can return to Telegram."

@app.get("/cancel")
def cancel(): return "Payment cancelled."

@app.get("/buy/<click_id>")
def buy(click_id):
    conn=db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.clicks WHERE click_id=%s",(click_id,)); click=cur.fetchone()
    finally: conn.close()
    if not click: return jsonify({"error":"invalid_click_id"}),404
    url=stripe_checkout(click_id,click["telegram_user_id"])
    if not url: return jsonify({"error":"stripe_not_configured","click_id":click_id}),503
    return jsonify({"checkout_url":url})

@app.post("/stripe/webhook")
def stripe_webhook():
    if not stripe or not STRIPE_WEBHOOK_SECRET: return jsonify({"error":"stripe_webhook_not_configured"}),503
    payload=request.data; sig=request.headers.get("Stripe-Signature","")
    try: event=stripe.Webhook.construct_event(payload,sig,STRIPE_WEBHOOK_SECRET)
    except Exception as exc: log.warning("Stripe webhook rejected: %s",exc); return jsonify({"error":"invalid_signature"}),400
    if event["type"]=="checkout.session.completed":
        session=event["data"]["object"]; metadata=session.get("metadata",{}); click_id=metadata.get("click_id"); user_id=metadata.get("telegram_user_id"); external_id=session.get("id"); amount=(session.get("amount_total") or 0)/100
        conn=db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT offer_id FROM public.clicks WHERE click_id=%s",(click_id,)); click=cur.fetchone(); commission=0
                if click and click["offer_id"]:
                    cur.execute("SELECT commission_percent FROM public.offers WHERE id=%s",(click["offer_id"],)); offer=cur.fetchone()
                    if offer: commission=amount*float(offer["commission_percent"])/100
                cur.execute("""INSERT INTO public.conversions(click_id,telegram_user_id,provider,external_id,amount,currency,commission,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (external_id) DO NOTHING""",
                (click_id,int(user_id) if user_id else None,"stripe",external_id,amount,session.get("currency","usd").upper(),commission,"approved"))
            conn.commit()
        finally: conn.close()
    return jsonify({"received":True})

@app.post("/telegram/webhook")
def telegram_webhook(): return jsonify({"ok":True})

def run_http():
    port=int(os.environ.get("PORT","10000")); app.run(host="0.0.0.0",port=port,use_reloader=False)

def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is required")
    if not DATABASE_URL: raise RuntimeError("DATABASE_URL is required")
    init_db(); application=Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start",start)); application.add_handler(CommandHandler("help",help_cmd)); application.add_handler(CommandHandler("link",link_cmd)); application.add_handler(CommandHandler("stats",stats_cmd)); application.add_handler(CommandHandler("stars",stars_cmd)); application.add_handler(CallbackQueryHandler(button_callback))
    threading.Thread(target=run_http,daemon=True).start(); log.info("StoneDigger starting with Supabase PostgreSQL, Oxshare only, and marketing command center")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__": main()
