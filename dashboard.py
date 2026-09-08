import html
import os
from functools import wraps

from flask import Blueprint, Response, request

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


dashboard_bp = Blueprint("dashboard", __name__)


def _db():
    url = os.environ.get("DATABASE_URL", "")
    if not psycopg or not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(url, row_factory=dict_row)


def _unauthorized():
    return Response(
        "Dashboard login required.",
        401,
        {"WWW-Authenticate": 'Basic realm="StoneDigger Admin Dashboard"'},
    )


def _auth_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        expected_user = os.environ.get("ADMIN_DASHBOARD_USER", "")
        expected_password = os.environ.get("ADMIN_DASHBOARD_PASSWORD", "")
        if (
            not expected_user
            or not expected_password
            or not auth
            or auth.username != expected_user
            or auth.password != expected_password
        ):
            return _unauthorized()
        return fn(*args, **kwargs)
    return wrapped


def _q(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def _one(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()


@dashboard_bp.get("/dashboard")
@_auth_required
def dashboard():
    conn = _db()
    try:
        with conn.cursor() as cur:
            kpis = _one(
                cur,
                """
                SELECT
                  (SELECT COUNT(*) FROM public.users) AS users,
                  (SELECT COUNT(*) FROM public.users WHERE created_at >= now() - interval '24 hours') AS users_24h,
                  (SELECT COUNT(*) FROM public.clicks) AS clicks,
                  (SELECT COUNT(*) FROM public.clicks WHERE created_at >= now() - interval '24 hours') AS clicks_24h,
                  (SELECT COUNT(*) FROM public.users WHERE referred_by_telegram_user_id IS NOT NULL) AS referrals,
                  (SELECT COUNT(*) FROM public.conversions WHERE status IN ('approved','paid')) AS conversions,
                  (SELECT COALESCE(SUM(commission),0) FROM public.conversions WHERE status IN ('approved','paid')) AS commission,
                  (SELECT COALESCE(SUM(amount),0) FROM public.conversions WHERE status IN ('approved','paid')) AS revenue
                """,
            )
            top_referrers = _q(
                cur,
                """
                SELECT COALESCE(u.username, u.first_name, 'Miner') AS name,
                       u.telegram_user_id,
                       COUNT(r.telegram_user_id) AS referrals,
                       COALESCE(u.activity_count,0) AS activity,
                       COALESCE(u.streak,0) AS streak
                FROM public.users u
                LEFT JOIN public.users r ON r.referred_by_telegram_user_id = u.telegram_user_id
                GROUP BY u.telegram_user_id, u.username, u.first_name, u.activity_count, u.streak
                ORDER BY referrals DESC, activity DESC, u.created_at ASC
                LIMIT 20
                """,
            )
            recent_users = _q(
                cur,
                """
                SELECT COALESCE(username, first_name, 'Miner') AS name,
                       telegram_user_id, activity_count, streak, referred_by_telegram_user_id, created_at, last_seen_at
                FROM public.users
                ORDER BY created_at DESC
                LIMIT 20
                """,
            )
            recent_clicks = _q(
                cur,
                """
                SELECT c.click_id, c.telegram_user_id, c.campaign_code, o.name AS offer, c.created_at
                FROM public.clicks c
                LEFT JOIN public.offers o ON o.id = c.offer_id
                ORDER BY c.created_at DESC
                LIMIT 20
                """,
            )
            conversions = _q(
                cur,
                """
                SELECT click_id, telegram_user_id, provider, amount, currency, commission, status, created_at
                FROM public.conversions
                ORDER BY created_at DESC
                LIMIT 20
                """,
            )
            offer = _one(
                cur,
                "SELECT name, affiliate_url, active FROM public.offers WHERE lower(name)='oxshare' ORDER BY id LIMIT 1",
            )
    finally:
        conn.close()

    def e(v):
        return html.escape(str(v if v is not None else ""))

    def money(v):
        try:
            return f"{float(v or 0):,.2f}"
        except Exception:
            return "0.00"

    top_rows = "".join(
        f"<tr><td>{i}</td><td>{e(r['name'])}</td><td>{r['referrals']}</td><td>{r['activity']}</td><td>{r['streak']}</td></tr>"
        for i, r in enumerate(top_referrers, 1)
    ) or '<tr><td colspan="5" class="muted">No referral data yet.</td></tr>'

    user_rows = "".join(
        f"<tr><td>{e(r['name'])}</td><td>{r['telegram_user_id']}</td><td>{r['activity_count'] or 0}</td>"
        f"<td>{r['streak'] or 0}</td><td>{'Yes' if r['referred_by_telegram_user_id'] else 'No'}</td>"
        f"<td>{e(r['created_at'])}</td></tr>"
        for r in recent_users
    ) or '<tr><td colspan="6" class="muted">No users yet.</td></tr>'

    click_rows = "".join(
        f"<tr><td><code>{e(r['click_id'])}</code></td><td>{r['telegram_user_id']}</td>"
        f"<td>{e(r['campaign_code'])}</td><td>{e(r['offer'] or '—')}</td><td>{e(r['created_at'])}</td></tr>"
        for r in recent_clicks
    ) or '<tr><td colspan="5" class="muted">No clicks yet.</td></tr>'

    conversion_rows = "".join(
        f"<tr><td><code>{e(r['click_id'])}</code></td><td>{r['telegram_user_id']}</td><td>{e(r['provider'])}</td>"
        f"<td>{money(r['amount'])} {e(r['currency'])}</td><td>{money(r['commission'])}</td>"
        f"<td>{e(r['status'])}</td><td>{e(r['created_at'])}</td></tr>"
        for r in conversions
    ) or '<tr><td colspan="7" class="muted">No conversions yet.</td></tr>'

    offer_html = "<span class='pill off'>Not found</span>"
    if offer:
        state = "ACTIVE" if offer["active"] else "INACTIVE"
        offer_html = f"<span class='pill {'on' if offer['active'] else 'off'}'>{state}</span>"
        offer_html += f" <span class='offer-url'>{e(offer['affiliate_url'])}</span>"

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StoneDigger Admin Dashboard</title>
<style>
:root{{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:#0b1020;color:#eef2ff}}
body{{margin:0;background:linear-gradient(180deg,#0b1020,#131a2e);min-height:100vh}}
.container{{max-width:1250px;margin:0 auto;padding:28px}}
.header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px}}
h1{{margin:0;font-size:30px}} .sub{{color:#9aa6c1;margin-top:7px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}}
.card{{background:#151d33;border:1px solid #293552;border-radius:16px;padding:18px;box-shadow:0 10px 30px rgba(0,0,0,.18)}}
.kpi{{font-size:30px;font-weight:800;margin-top:7px}} .label{{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#8f9ab4}}
.small{{font-size:12px;color:#91a0bd;margin-top:5px}}
.section{{margin-top:18px}} .section h2{{font-size:18px;margin:0 0 10px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:10px 9px;border-bottom:1px solid #26314b;text-align:left;white-space:nowrap}} th{{color:#aab6d1;font-size:11px;text-transform:uppercase;letter-spacing:.06em}}
.scroll{{overflow:auto;border:1px solid #293552;border-radius:14px}} code{{color:#b9c6ff}} .muted{{color:#73809d;text-align:center;padding:20px}}
.pill{{display:inline-block;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700}} .on{{background:#163f2b;color:#77e3a8}} .off{{background:#40212b;color:#ff9eaf}}
.offer-url{{color:#9eacd0;font-size:12px;word-break:break-all}}
@media(max-width:900px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:600px){{.container{{padding:16px}}.grid{{grid-template-columns:1fr}}h1{{font-size:24px}}}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div><h1>⛏️ StoneDigger Admin</h1><div class="sub">Sales, referrals, engagement and conversion monitoring</div></div>
    <div>{offer_html}</div>
  </div>
  <div class="grid">
    <div class="card"><div class="label">Total Users</div><div class="kpi">{kpis['users']}</div><div class="small">+{kpis['users_24h']} in 24h</div></div>
    <div class="card"><div class="label">Total Clicks</div><div class="kpi">{kpis['clicks']}</div><div class="small">+{kpis['clicks_24h']} in 24h</div></div>
    <div class="card"><div class="label">Referrals</div><div class="kpi">{kpis['referrals']}</div><div class="small">Tracked invited users</div></div>
    <div class="card"><div class="label">Conversions</div><div class="kpi">{kpis['conversions']}</div><div class="small">Approved / paid</div></div>
    <div class="card"><div class="label">Revenue</div><div class="kpi">{money(kpis['revenue'])}</div><div class="small">Recorded paid/approved</div></div>
    <div class="card"><div class="label">Commission</div><div class="kpi">{money(kpis['commission'])}</div><div class="small">Recorded commission</div></div>
    <div class="card"><div class="label">Click → Conversion</div><div class="kpi">{(float(kpis['conversions'])/float(kpis['clicks'])*100 if kpis['clicks'] else 0):.1f}%</div><div class="small">Overall conversion rate</div></div>
    <div class="card"><div class="label">Referral → User</div><div class="kpi">{(float(kpis['referrals'])/float(kpis['users'])*100 if kpis['users'] else 0):.1f}%</div><div class="small">Users acquired by referral</div></div>
  </div>

  <div class="section card"><h2>🏆 Top Referrers</h2><div class="scroll"><table><thead><tr><th>#</th><th>User</th><th>Referrals</th><th>Activity</th><th>Streak</th></tr></thead><tbody>{top_rows}</tbody></table></div></div>

  <div class="section card"><h2>👥 Recent Users</h2><div class="scroll"><table><thead><tr><th>User</th><th>Telegram ID</th><th>Activity</th><th>Streak</th><th>Referred</th><th>Created</th></tr></thead><tbody>{user_rows}</tbody></table></div></div>

  <div class="section card"><h2>🔗 Recent Clicks</h2><div class="scroll"><table><thead><tr><th>Click ID</th><th>Telegram ID</th><th>Campaign</th><th>Offer</th><th>Created</th></tr></thead><tbody>{click_rows}</tbody></table></div></div>

  <div class="section card"><h2>💰 Recent Conversions</h2><div class="scroll"><table><thead><tr><th>Click ID</th><th>Telegram ID</th><th>Provider</th><th>Amount</th><th>Commission</th><th>Status</th><th>Created</th></tr></thead><tbody>{conversion_rows}</tbody></table></div></div>
</div>
</body>
</html>"""
    return Response(page, mimetype="text/html")
