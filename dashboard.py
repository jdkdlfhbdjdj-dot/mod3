import html
import os
from collections import defaultdict
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


def _safe_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value):
    return f"{_safe_float(value):,.2f}"


def _escape(value):
    return html.escape(str(value if value is not None else ""))


def _date_label(value):
    return value.strftime("%b %d") if hasattr(value, "strftime") else str(value)[:10]


def _svg_line_chart(rows, keys, labels, title, value_format="number"):
    width, height = 900, 280
    pad_l, pad_r, pad_t, pad_b = 52, 20, 42, 42
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    values = [float(r[k] or 0) for r in rows for k in keys]
    max_v = max(values) if values else 0
    if max_v <= 0:
        max_v = 1
    colors = ["#7dd3fc", "#c4b5fd", "#86efac", "#f9a8d4"]
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_escape(title)}">']
    parts.append(f'<text x="{pad_l}" y="24" class="chart-title">{_escape(title)}</text>')
    for i in range(5):
        y = pad_t + plot_h * i / 4
        value = max_v * (1 - i / 4)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="gridline"/>')
        txt = f"{value:,.0f}" if value_format == "number" else f"{value:,.2f}"
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" text-anchor="end" class="axis">{txt}</text>')
    n = max(len(rows), 1)
    for idx, row in enumerate(rows):
        x = pad_l + (plot_w * idx / max(n - 1, 1))
        if idx in {0, len(rows)//2, len(rows)-1}:
            parts.append(f'<text x="{x:.1f}" y="{height-14}" text-anchor="middle" class="axis">{_escape(_date_label(row["period"]))}</text>')
    for series, key in enumerate(keys):
        points = []
        for idx, row in enumerate(rows):
            x = pad_l + (plot_w * idx / max(n - 1, 1))
            y = pad_t + plot_h * (1 - float(row[key] or 0) / max_v)
            points.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{colors[series % len(colors)]}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
        for idx, row in enumerate(rows):
            x = pad_l + (plot_w * idx / max(n - 1, 1))
            y = pad_t + plot_h * (1 - float(row[key] or 0) / max_v)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.7" fill="{colors[series % len(colors)]}"/>')
    legend_x = pad_l
    for idx, label in enumerate(labels):
        x = legend_x + idx * 125
        parts.append(f'<line x1="{x}" y1="{height-2}" x2="{x+20}" y2="{height-2}" stroke="{colors[idx % len(colors)]}" stroke-width="3"/>')
        parts.append(f'<text x="{x+26}" y="{height+2}" class="axis">{_escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_bar_chart(rows, value_key, title, money=False):
    width, height = 900, 280
    pad_l, pad_r, pad_t, pad_b = 52, 20, 42, 42
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    values = [float(r[value_key] or 0) for r in rows]
    max_v = max(values) if values else 0
    if max_v <= 0:
        max_v = 1
    bar_w = max(plot_w / max(len(rows), 1) - 5, 3)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_escape(title)}">']
    parts.append(f'<text x="{pad_l}" y="24" class="chart-title">{_escape(title)}</text>')
    for i in range(5):
        y = pad_t + plot_h * i / 4
        value = max_v * (1 - i / 4)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="gridline"/>')
        txt = f"{value:,.2f}" if money else f"{value:,.0f}"
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" text-anchor="end" class="axis">{txt}</text>')
    for idx, row in enumerate(rows):
        x = pad_l + plot_w * idx / max(len(rows), 1) + 2
        val = float(row[value_key] or 0)
        bar_h = plot_h * val / max_v
        y = pad_t + plot_h - bar_h
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4" class="bar"/>')
        if idx in {0, len(rows)//2, len(rows)-1}:
            parts.append(f'<text x="{x+bar_w/2:.1f}" y="{height-14}" text-anchor="middle" class="axis">{_escape(_date_label(row["period"]))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _tree_html(users):
    children = defaultdict(list)
    roots = []
    for user in users:
        parent = user["referred_by_telegram_user_id"]
        if parent is None:
            roots.append(user)
        else:
            children[parent].append(user)

    def node(user, depth=0):
        uid = user["telegram_user_id"]
        name = _escape(user["name"])
        status = _escape(user["conversion_status"] or "No conversion")
        cls = "paid" if status == "paid" else ("converted" if status == "approved" else "none")
        kids = children.get(uid, [])
        body = (
            f'<span class="tree-name">{name}</span>'
            f'<span class="tree-meta">ID {uid} · {user["referrals"]} refs · {user["activity"]} activity · {user["streak"]} streak · '
            f'<b class="status {cls}">{status}</b></span>'
        )
        if kids:
            inner = "".join(node(child, depth + 1) for child in kids)
            return f'<details class="tree-node" {"open" if depth == 0 else ""}><summary>{body}</summary><div class="tree-children">{inner}</div></details>'
        return f'<div class="tree-leaf">{body}</div>'

    if not roots:
        return '<div class="muted">No users yet.</div>'
    return "".join(node(root) for root in roots)


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
                  (SELECT COUNT(*) FROM public.conversions) AS conversions_all,
                  (SELECT COUNT(*) FROM public.conversions WHERE status='approved') AS approved_conversions,
                  (SELECT COUNT(*) FROM public.conversions WHERE status='paid') AS paid_conversions,
                  (SELECT COALESCE(SUM(commission),0) FROM public.conversions WHERE status IN ('approved','paid')) AS commission,
                  (SELECT COALESCE(SUM(amount),0) FROM public.conversions WHERE status IN ('approved','paid')) AS revenue
                """,
            )
            daily = _q(
                cur,
                """
                WITH days AS (
                  SELECT generate_series(current_date - 29, current_date, interval '1 day')::date AS period
                ),
                u AS (SELECT created_at::date period, count(*) value FROM public.users WHERE created_at >= current_date - 29 GROUP BY 1),
                c AS (SELECT created_at::date period, count(*) value FROM public.clicks WHERE created_at >= current_date - 29 GROUP BY 1),
                r AS (SELECT created_at::date period, count(*) value FROM public.users WHERE referred_by_telegram_user_id IS NOT NULL AND created_at >= current_date - 29 GROUP BY 1),
                v AS (SELECT created_at::date period, count(*) value FROM public.conversions WHERE created_at >= current_date - 29 GROUP BY 1),
                p AS (SELECT created_at::date period, COALESCE(sum(amount),0) value FROM public.conversions WHERE status IN ('approved','paid') AND created_at >= current_date - 29 GROUP BY 1),
                m AS (SELECT created_at::date period, COALESCE(sum(commission),0) value FROM public.conversions WHERE status IN ('approved','paid') AND created_at >= current_date - 29 GROUP BY 1)
                SELECT d.period, COALESCE(u.value,0) users, COALESCE(c.value,0) clicks,
                       COALESCE(r.value,0) referrals, COALESCE(v.value,0) conversions,
                       COALESCE(p.value,0) revenue, COALESCE(m.value,0) commission
                FROM days d LEFT JOIN u USING(period) LEFT JOIN c USING(period) LEFT JOIN r USING(period)
                LEFT JOIN v USING(period) LEFT JOIN p USING(period) LEFT JOIN m USING(period) ORDER BY d.period
                """,
            )
            weekly = _q(
                cur,
                """
                WITH weeks AS (
                  SELECT generate_series(date_trunc('week', current_date)::date - interval '11 weeks', date_trunc('week', current_date)::date, interval '1 week')::date period
                ),
                v AS (SELECT date_trunc('week',created_at)::date period, count(*) value FROM public.conversions WHERE created_at >= date_trunc('week',current_date)-interval '11 weeks' GROUP BY 1),
                p AS (SELECT date_trunc('week',created_at)::date period, COALESCE(sum(amount),0) value FROM public.conversions WHERE status IN ('approved','paid') AND created_at >= date_trunc('week',current_date)-interval '11 weeks' GROUP BY 1),
                m AS (SELECT date_trunc('week',created_at)::date period, COALESCE(sum(commission),0) value FROM public.conversions WHERE status IN ('approved','paid') AND created_at >= date_trunc('week',current_date)-interval '11 weeks' GROUP BY 1)
                SELECT w.period, COALESCE(v.value,0) conversions, COALESCE(p.value,0) revenue, COALESCE(m.value,0) commission
                FROM weeks w LEFT JOIN v USING(period) LEFT JOIN p USING(period) LEFT JOIN m USING(period) ORDER BY w.period
                """,
            )
            funnel = _one(
                cur,
                """
                SELECT
                  (SELECT COUNT(*) FROM public.users) users,
                  (SELECT COUNT(*) FROM public.clicks) clicks,
                  (SELECT COUNT(*) FROM public.conversions) conversions,
                  (SELECT COUNT(*) FROM public.conversions WHERE status IN ('approved','paid')) paid_conversions
                """,
            )
            top_referrers = _q(
                cur,
                """
                SELECT COALESCE(u.username, u.first_name, 'Miner') AS name,
                       u.telegram_user_id, COUNT(r.telegram_user_id) AS referrals,
                       COALESCE(u.activity_count,0) AS activity, COALESCE(u.streak,0) AS streak
                FROM public.users u
                LEFT JOIN public.users r ON r.referred_by_telegram_user_id = u.telegram_user_id
                GROUP BY u.telegram_user_id, u.username, u.first_name, u.activity_count, u.streak, u.created_at
                ORDER BY referrals DESC, activity DESC, u.created_at ASC LIMIT 20
                """,
            )
            users = _q(
                cur,
                """
                SELECT COALESCE(u.username, u.first_name, 'Miner') AS name, u.telegram_user_id,
                       COALESCE(u.activity_count,0) activity, COALESCE(u.streak,0) streak,
                       u.referred_by_telegram_user_id, u.created_at,
                       COUNT(r.telegram_user_id) referrals,
                       COALESCE(MAX(CASE WHEN c.status='paid' THEN 'paid' WHEN c.status='approved' THEN 'approved' END),'') conversion_status
                FROM public.users u
                LEFT JOIN public.users r ON r.referred_by_telegram_user_id=u.telegram_user_id
                LEFT JOIN public.conversions c ON c.telegram_user_id=u.telegram_user_id
                GROUP BY u.telegram_user_id,u.username,u.first_name,u.activity_count,u.streak,u.referred_by_telegram_user_id,u.created_at
                ORDER BY u.created_at DESC
                """,
            )
            recent_users = users[:20]
            recent_clicks = _q(
                cur,
                """
                SELECT c.click_id,c.telegram_user_id,c.campaign_code,o.name offer,c.created_at
                FROM public.clicks c LEFT JOIN public.offers o ON o.id=c.offer_id
                ORDER BY c.created_at DESC LIMIT 20
                """,
            )
            conversions = _q(
                cur,
                """
                SELECT click_id,telegram_user_id,provider,amount,currency,commission,status,created_at
                FROM public.conversions ORDER BY created_at DESC LIMIT 20
                """,
            )
            offer = _one(cur, "SELECT name,affiliate_url,active FROM public.offers WHERE lower(name)='oxshare' ORDER BY id LIMIT 1")
    finally:
        conn.close()

    daily_chart = _svg_line_chart(daily, ["users","clicks","referrals"], ["Users","Clicks","Referrals"], "Daily acquisition — last 30 days")
    conversion_chart = _svg_line_chart(daily, ["conversions"], ["Conversions"], "Daily conversions — last 30 days")
    revenue_chart = _svg_line_chart(daily, ["revenue","commission"], ["Revenue","Commission"], "Daily sales value — last 30 days", "money")
    weekly_chart = _svg_bar_chart(weekly, "revenue", "Weekly revenue — last 12 weeks", True)

    users_count = int(funnel["users"] or 0)
    clicks_count = int(funnel["clicks"] or 0)
    conv_count = int(funnel["conversions"] or 0)
    paid_count = int(funnel["paid_conversions"] or 0)
    funnel_steps = [("Users", users_count), ("Oxshare clicks", clicks_count), ("Conversions", conv_count), ("Approved / paid", paid_count)]
    max_funnel = max(users_count, 1)
    funnel_html = "".join(
        f'<div class="funnel-row"><div class="funnel-label"><b>{_escape(label)}</b><span>{value:,} · {(value/max_funnel*100):.1f}%</span></div><div class="funnel-track"><div class="funnel-fill" style="width:{min(value/max_funnel*100,100):.1f}%"></div></div></div>'
        for label, value in funnel_steps
    )

    top_rows = "".join(
        f'<tr><td>{i}</td><td>{_escape(r["name"])}</td><td>{r["referrals"]}</td><td>{r["activity"]}</td><td>{r["streak"]}</td></tr>'
        for i, r in enumerate(top_referrers, 1)
    ) or '<tr><td colspan="5" class="muted">No referral data yet.</td></tr>'
    user_rows = "".join(
        f'<tr><td>{_escape(r["name"])}</td><td>{r["telegram_user_id"]}</td><td>{r["activity"]}</td><td>{r["streak"]}</td><td>{"Yes" if r["referred_by_telegram_user_id"] else "No"}</td><td>{_escape(r["created_at"])}</td></tr>'
        for r in recent_users
    ) or '<tr><td colspan="6" class="muted">No users yet.</td></tr>'
    click_rows = "".join(
        f'<tr><td><code>{_escape(r["click_id"])}</code></td><td>{r["telegram_user_id"]}</td><td>{_escape(r["campaign_code"])}</td><td>{_escape(r["offer"] or "—")}</td><td>{_escape(r["created_at"])}</td></tr>'
        for r in recent_clicks
    ) or '<tr><td colspan="5" class="muted">No clicks yet.</td></tr>'
    conversion_rows = "".join(
        f'<tr><td><code>{_escape(r["click_id"])}</code></td><td>{r["telegram_user_id"]}</td><td>{_escape(r["provider"])}</td><td>{_money(r["amount"])} {_escape(r["currency"])}</td><td>{_money(r["commission"])}</td><td><span class="status {"paid" if r["status"]=="paid" else "converted" if r["status"]=="approved" else "none"}">{_escape(r["status"])}</span></td><td>{_escape(r["created_at"])}</td></tr>'
        for r in conversions
    ) or '<tr><td colspan="7" class="muted">No conversions yet.</td></tr>'

    offer_html = '<span class="pill off">Not found</span>'
    if offer:
        state = "ACTIVE" if offer["active"] else "INACTIVE"
        offer_html = f'<span class="pill {"on" if offer["active"] else "off"}">{state}</span> <span class="offer-url">{_escape(offer["affiliate_url"])}</span>'

    referral_tree = _tree_html(users)
    click_rate = clicks_count / users_count * 100 if users_count else 0
    conversion_rate = paid_count / clicks_count * 100 if clicks_count else 0
    referral_rate = int(funnel["users"] or 0) and (sum(1 for u in users if u["referred_by_telegram_user_id"] is not None) / users_count * 100) or 0

    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StoneDigger Admin Dashboard</title>
<style>
:root{{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:#0b1020;color:#eef2ff}}
body{{margin:0;background:linear-gradient(180deg,#0b1020,#131a2e);min-height:100vh}} .container{{max-width:1350px;margin:0 auto;padding:28px}}
.header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px}} h1{{margin:0;font-size:30px}} .sub{{color:#9aa6c1;margin-top:7px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}} .card{{background:#151d33;border:1px solid #293552;border-radius:16px;padding:18px;box-shadow:0 10px 30px rgba(0,0,0,.18)}}
.kpi{{font-size:30px;font-weight:800;margin-top:7px}} .label{{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#8f9ab4}} .small{{font-size:12px;color:#91a0bd;margin-top:5px}}
.section{{margin-top:18px}} .section h2{{font-size:18px;margin:0 0 12px}} .chart-grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}} .chart{{overflow:auto}}
.chart svg{{width:100%;min-width:620px;height:auto}} .chart-title{{fill:#eef2ff;font-size:14px;font-weight:700}} .axis{{fill:#8290ad;font-size:10px}} .gridline{{stroke:#293552;stroke-width:1}} .bar{{fill:#7dd3fc}}
.funnel-row{{margin:14px 0}} .funnel-label{{display:flex;justify-content:space-between;color:#b8c3db;font-size:13px;margin-bottom:6px}} .funnel-track{{height:14px;background:#202b45;border-radius:999px;overflow:hidden}} .funnel-fill{{height:100%;background:linear-gradient(90deg,#7dd3fc,#a78bfa);border-radius:999px}}
.tree-node,.tree-leaf{{border-left:1px solid #34425f;margin:7px 0;padding-left:12px}} summary{{cursor:pointer;list-style:none}} summary::-webkit-details-marker{{display:none}} summary:before{{content:'＋';display:inline-block;width:18px;color:#7dd3fc}} details[open]>summary:before{{content:'−'}} .tree-leaf:before{{content:'•';display:inline-block;width:18px;color:#64748b}} .tree-name{{font-weight:700}} .tree-meta{{display:block;color:#8290ad;font-size:11px;margin-top:3px}} .tree-children{{margin-left:10px}}
.status{{display:inline-block;padding:3px 7px;border-radius:999px;font-size:10px;font-weight:800;text-transform:uppercase}} .status.paid{{background:#163f2b;color:#77e3a8}} .status.converted{{background:#263d59;color:#9fd4ff}} .status.none{{background:#34283a;color:#b9a8c9}}
.pill{{display:inline-block;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700}} .on{{background:#163f2b;color:#77e3a8}} .off{{background:#40212b;color:#ff9eaf}} .offer-url{{color:#9eacd0;font-size:12px;word-break:break-all}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:10px 9px;border-bottom:1px solid #26314b;text-align:left;white-space:nowrap}} th{{color:#aab6d1;font-size:11px;text-transform:uppercase;letter-spacing:.06em}} .scroll{{overflow:auto;border:1px solid #293552;border-radius:14px}} code{{color:#b9c6ff}} .muted{{color:#73809d;text-align:center;padding:20px}}
.badges{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}} .badge{{background:#202b45;border:1px solid #33415f;border-radius:999px;padding:5px 9px;color:#aab6d1;font-size:11px}}
@media(max-width:1000px){{.grid{{grid-template-columns:repeat(2,1fr)}}.chart-grid{{grid-template-columns:1fr}}}} @media(max-width:600px){{.container{{padding:16px}}.grid{{grid-template-columns:1fr}}h1{{font-size:24px}}}}
</style></head><body><div class="container">
<div class="header"><div><h1>⛏️ StoneDigger Admin</h1><div class="sub">Sales, referrals, engagement and conversion monitoring</div><div class="badges"><span class="badge">Daily: 30 days</span><span class="badge">Weekly: 12 weeks</span><span class="badge">Oxshare tracking</span></div></div><div>{offer_html}</div></div>
<div class="grid">
<div class="card"><div class="label">Total Users</div><div class="kpi">{kpis['users']}</div><div class="small">+{kpis['users_24h']} in 24h</div></div>
<div class="card"><div class="label">Total Clicks</div><div class="kpi">{kpis['clicks']}</div><div class="small">+{kpis['clicks_24h']} in 24h</div></div>
<div class="card"><div class="label">Referrals</div><div class="kpi">{kpis['referrals']}</div><div class="small">{referral_rate:.1f}% of users</div></div>
<div class="card"><div class="label">Conversions</div><div class="kpi">{kpis['conversions_all']}</div><div class="small">{kpis['approved_conversions']} approved · {kpis['paid_conversions']} paid</div></div>
<div class="card"><div class="label">Revenue</div><div class="kpi">{_money(kpis['revenue'])}</div><div class="small">Approved / paid only</div></div>
<div class="card"><div class="label">Commission</div><div class="kpi">{_money(kpis['commission'])}</div><div class="small">Recorded commission</div></div>
<div class="card"><div class="label">User → Click</div><div class="kpi">{click_rate:.1f}%</div><div class="small">Users generating tracked clicks</div></div>
<div class="card"><div class="label">Click → Paid</div><div class="kpi">{conversion_rate:.1f}%</div><div class="small">Paid/approved ÷ clicks</div></div>
</div>
<div class="section card"><h2>📈 Daily performance</h2><div class="chart-grid"><div class="chart">{daily_chart}</div><div class="chart">{conversion_chart}</div><div class="chart">{revenue_chart}</div><div class="chart">{weekly_chart}</div></div></div>
<div class="section card"><h2>🎯 Conversion funnel</h2><div>{funnel_html}</div><div class="small">The funnel uses tracked StoneDigger users, Oxshare clicks, conversion records, and approved/paid conversion records. No conversion is invented from a click.</div></div>
<div class="section card"><h2>🌳 Referral trees</h2><div class="small">Expand any user to see the people they directly referred. Conversion status is based on the conversion records for that Telegram user.</div><div style="margin-top:14px">{referral_tree}</div></div>
<div class="section card"><h2>🏆 Top Referrers</h2><div class="scroll"><table><thead><tr><th>#</th><th>User</th><th>Referrals</th><th>Activity</th><th>Streak</th></tr></thead><tbody>{top_rows}</tbody></table></div></div>
<div class="section card"><h2>👥 Recent Users</h2><div class="scroll"><table><thead><tr><th>User</th><th>Telegram ID</th><th>Activity</th><th>Streak</th><th>Referred</th><th>Created</th></tr></thead><tbody>{user_rows}</tbody></table></div></div>
<div class="section card"><h2>🔗 Recent Clicks</h2><div class="scroll"><table><thead><tr><th>Click ID</th><th>Telegram ID</th><th>Campaign</th><th>Offer</th><th>Created</th></tr></thead><tbody>{click_rows}</tbody></table></div></div>
<div class="section card"><h2>💰 Recent Conversions</h2><div class="scroll"><table><thead><tr><th>Click ID</th><th>Telegram ID</th><th>Provider</th><th>Amount</th><th>Commission</th><th>Status</th><th>Created</th></tr></thead><tbody>{conversion_rows}</tbody></table></div></div>
</div></body></html>'''
    return Response(page, mimetype="text/html")
