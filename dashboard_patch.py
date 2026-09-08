import csv
import html
import io
from datetime import datetime, timedelta, timezone

from flask import Response, jsonify, request
from telegram import Update

import app as core


def _e(v):
    return html.escape(str(v if v is not None else ""))


def _n(v):
    return int(v or 0)


def _money(v):
    try:
        return f"{float(v or 0):,.2f}"
    except Exception:
        return "0.00"


def enhanced_start(update, context):
    # Telegram allows one start argument. We support ref_u<ID>__c_<campaign>.
    user = update.effective_user
    raw = context.args[0] if context.args else None
    referral = raw
    campaign = "default"
    if raw and "__c_" in raw:
        referral, campaign = raw.split("__c_", 1)
        campaign = campaign[:80] or "default"
    core.save_user(user, referrer=referral)
    offer = core.get_oxshare_offer()
    if offer:
        core.create_click(user.id, campaign_code=campaign, offer_id=offer["id"])
    stats = core.get_user_stats(user.id)
    await update.message.reply_text(
        core.welcome_text(user, stats),
        parse_mode="Markdown",
        reply_markup=core.main_keyboard(offer),
    )


def _series(conn, days=30):
    with conn.cursor() as cur:
        cur.execute("""
            WITH days AS (
              SELECT generate_series(current_date - (%s - 1) * interval '1 day', current_date, interval '1 day')::date AS d
            )
            SELECT d,
              (SELECT COUNT(*) FROM public.users u WHERE u.created_at::date=d) users,
              (SELECT COUNT(*) FROM public.clicks c WHERE c.created_at::date=d) clicks,
              (SELECT COUNT(*) FROM public.users u WHERE u.referred_by_telegram_user_id IS NOT NULL AND u.created_at::date=d) referrals,
              (SELECT COUNT(*) FROM public.conversions x WHERE x.status IN ('approved','paid') AND x.created_at::date=d) conversions,
              (SELECT COALESCE(SUM(x.amount),0) FROM public.conversions x WHERE x.status IN ('approved','paid') AND x.created_at::date=d) revenue
            FROM days ORDER BY d
        """, (days,))
        return cur.fetchall()


def _campaigns(conn):
    with conn.cursor() as cur:
        cur.execute("""
          SELECT campaign_code, COUNT(*) clicks,
                 COUNT(DISTINCT telegram_user_id) users
          FROM public.clicks
          GROUP BY campaign_code
          ORDER BY clicks DESC, campaign_code
          LIMIT 30
        """)
        return cur.fetchall()


def _ref_tree(conn):
    with conn.cursor() as cur:
        cur.execute("""
          SELECT u.telegram_user_id AS referrer_id,
                 COALESCE(u.username,u.first_name,'Miner') AS referrer,
                 r.telegram_user_id AS child_id,
                 COALESCE(r.username,r.first_name,'Miner') AS child,
                 COALESCE(r.activity_count,0) activity,
                 COALESCE(r.streak,0) streak,
                 r.created_at
          FROM public.users u
          JOIN public.users r ON r.referred_by_telegram_user_id=u.telegram_user_id
          ORDER BY u.created_at ASC, r.created_at ASC
        """)
        return cur.fetchall()


def _dashboard_data():
    conn = core.db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
              SELECT
                (SELECT COUNT(*) FROM public.users) users,
                (SELECT COUNT(*) FROM public.users WHERE created_at >= now()-interval '24 hours') users_24h,
                (SELECT COUNT(*) FROM public.clicks) clicks,
                (SELECT COUNT(*) FROM public.clicks WHERE created_at >= now()-interval '24 hours') clicks_24h,
                (SELECT COUNT(*) FROM public.users WHERE referred_by_telegram_user_id IS NOT NULL) referrals,
                (SELECT COUNT(*) FROM public.conversions WHERE status IN ('approved','paid')) conversions,
                (SELECT COALESCE(SUM(amount),0) FROM public.conversions WHERE status IN ('approved','paid')) revenue,
                (SELECT COALESCE(SUM(commission),0) FROM public.conversions WHERE status IN ('approved','paid')) commission
            """)
            k = cur.fetchone()
            cur.execute("""
              SELECT COALESCE(u.username,u.first_name,'Miner') name,
                     u.telegram_user_id,
                     COUNT(r.telegram_user_id) referrals,
                     COALESCE(u.activity_count,0) activity,
                     COALESCE(u.streak,0) streak
              FROM public.users u
              LEFT JOIN public.users r ON r.referred_by_telegram_user_id=u.telegram_user_id
              GROUP BY u.telegram_user_id,u.username,u.first_name,u.activity_count,u.streak
              ORDER BY referrals DESC, activity DESC LIMIT 20
            """)
            top = cur.fetchall()
            cur.execute("SELECT COALESCE(name,'Miner') name, telegram_user_id, activity_count, streak, referred_by_telegram_user_id, created_at FROM public.users ORDER BY created_at DESC LIMIT 25")
            users = cur.fetchall()
            cur.execute("SELECT c.click_id,c.telegram_user_id,c.campaign_code,COALESCE(o.name,'—') offer,c.created_at FROM public.clicks c LEFT JOIN public.offers o ON o.id=c.offer_id ORDER BY c.created_at DESC LIMIT 25")
            clicks = cur.fetchall()
            cur.execute("SELECT click_id,telegram_user_id,provider,amount,currency,commission,status,created_at FROM public.conversions ORDER BY created_at DESC LIMIT 25")
            conversions = cur.fetchall()
            cur.execute("SELECT name,affiliate_url,active FROM public.offers WHERE lower(name)='oxshare' ORDER BY id LIMIT 1")
            offer = cur.fetchone()
            series = _series(conn, 30)
            campaigns = _campaigns(conn)
            tree = _ref_tree(conn)
    finally:
        conn.close()
    return k, top, users, clicks, conversions, offer, series, campaigns, tree


def dashboard_export():
    auth = core.dashboard_auth()
    if auth:
        return auth
    conn = core.db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_user_id,username,first_name,activity_count,streak,referred_by_telegram_user_id,created_at,last_seen_at FROM public.users ORDER BY created_at DESC")
            rows = cur.fetchall()
    finally:
        conn.close()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["telegram_user_id","username","first_name","activity","streak","referred_by","created_at","last_seen_at"])
    for r in rows:
        writer.writerow([r[k] for k in ["telegram_user_id","username","first_name","activity_count","streak","referred_by_telegram_user_id","created_at","last_seen_at"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment; filename=stonedigger-users.csv"})


def enhanced_dashboard():
    auth = core.dashboard_auth()
    if auth:
        return auth
    try:
        k, top, users, clicks, conversions, offer, series, campaigns, tree = _dashboard_data()
    except Exception as exc:
        core.log.exception("Enhanced dashboard query failed")
        return jsonify({"error":"dashboard_query_failed","detail":str(exc)}),500

    def n(v): return _n(v)
    def e(v): return _e(v)
    def money(v): return _money(v)
    def bar(value, maxv):
        width = 0 if not maxv else max(2, int(float(value or 0)/maxv*100))
        return f'<div class="bar"><span style="width:{width}%"></span></div>'

    maxv = max([max(n(r["users"]),n(r["clicks"]),n(r["referrals"]),n(r["conversions"])) for r in series] + [1])
    chart_rows = "".join(f"<tr><td>{e(r['d'])}</td><td>{n(r['users'])}</td><td>{n(r['clicks'])}</td><td>{n(r['referrals'])}</td><td>{n(r['conversions'])}</td><td>{money(r['revenue'])}</td></tr>" for r in series)
    chart_visual = "".join(f"<div class='day'><div class='bars'><i style='height:{max(2,int(n(r['users'])/maxv*120))}px' title='Users {n(r['users'])}'></i><i style='height:{max(2,int(n(r['clicks'])/maxv*120))}px' title='Clicks {n(r['clicks'])}'></i><i style='height:{max(2,int(n(r['referrals'])/maxv*120))}px' title='Referrals {n(r['referrals'])}'></i><i style='height:{max(2,int(n(r['conversions'])/maxv*120))}px' title='Conversions {n(r['conversions'])}'></i></div><small>{str(r['d'])[-5:]}</small></div>" for r in series)
    top_rows = "".join(f"<tr><td>{i}</td><td>{e(r['name'])}</td><td>{n(r['referrals'])}</td><td>{n(r['activity'])}</td><td>{n(r['streak'])}</td></tr>" for i,r in enumerate(top,1)) or '<tr><td colspan=5 class=muted>No referral data yet.</td></tr>'
    user_rows = "".join(f"<tr><td>{e(r['name'])}</td><td>{r['telegram_user_id']}</td><td>{n(r['activity_count'])}</td><td>{n(r['streak'])}</td><td>{'Yes' if r['referred_by_telegram_user_id'] else 'No'}</td><td>{e(r['created_at'])}</td></tr>" for r in users)
    click_rows = "".join(f"<tr><td><code>{e(r['click_id'])}</code></td><td>{r['telegram_user_id']}</td><td>{e(r['campaign_code'])}</td><td>{e(r['offer'])}</td><td>{e(r['created_at'])}</td></tr>" for r in clicks)
    conversion_rows = "".join(f"<tr><td><code>{e(r['click_id'])}</code></td><td>{r['telegram_user_id']}</td><td>{e(r['provider'])}</td><td>{money(r['amount'])} {e(r['currency'])}</td><td>{money(r['commission'])}</td><td>{e(r['status'])}</td></tr>" for r in conversions) or '<tr><td colspan=6 class=muted>No conversions yet.</td></tr>'

    funnel = [("Telegram users",n(k["users"])),("Oxshare clicks",n(k["clicks"])),("Referrals",n(k["referrals"])),("Approved / paid",n(k["conversions"]))]
    funnel_html = "".join(f"<div class='funnel-row'><span>{e(label)}</span><b>{value}</b><div class='funnel-bar'><i style='width:{(value/max(n(k['users']),1))*100:.1f}%'></i></div></div>" for label,value in funnel)

    groups = {}
    for r in tree:
        groups.setdefault((r["referrer_id"],r["referrer"]),[]).append(r)
    tree_html = "".join(f"<details><summary>👤 {e(ref)} <strong>{len(rows)} referral(s)</strong></summary><div class='children'>" + "".join(f"<div>↳ <b>{e(r['child'])}</b> <span>ID {r['child_id']} · ⛏️{n(r['activity'])} · 🔥{n(r['streak'])}</span></div>" for r in rows) + "</div></details>" for (rid,ref),rows in groups.items()) or '<div class=muted>No referral tree yet.</div>'
    campaign_rows = "".join(f"<tr><td><code>{e(r['campaign_code'])}</code></td><td>{n(r['clicks'])}</td><td>{n(r['users'])}</td></tr>" for r in campaigns)
    offer_url = e(offer["affiliate_url"]) if offer else ""

    page=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>StoneDigger Command Center</title><style>
:root{{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:#090d18;color:#edf2ff}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(180deg,#090d18,#11182a);min-height:100vh}}.container{{max-width:1400px;margin:auto;padding:24px}}.header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:20px}}h1{{margin:0;font-size:30px}}h2{{font-size:18px;margin:0 0 12px}}.sub,.muted,.small{{color:#8f9bb5;font-size:12px}}.pill{{padding:6px 10px;border-radius:999px;background:#123b29;color:#79e0a4;font-size:11px;font-weight:800}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:#141c30;border:1px solid #283550;border-radius:16px;padding:17px;box-shadow:0 10px 30px #0004;margin-top:16px}}.kpi{{font-size:30px;font-weight:850;margin:6px 0}}.section-grid{{display:grid;grid-template-columns:1.35fr .65fr;gap:16px}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:9px;border-bottom:1px solid #25304a;text-align:left;white-space:nowrap}}th{{font-size:10px;color:#9daac4;text-transform:uppercase}}.scroll{{overflow:auto}}code{{color:#b9c7ff}}.chart{{height:170px;display:flex;align-items:end;gap:3px;overflow:hidden;padding:10px 2px 0;border-bottom:1px solid #2a3550}}.day{{min-width:25px;flex:1;text-align:center}}.bars{{height:130px;display:flex;align-items:end;justify-content:center;gap:2px}}.bars i{{width:4px;display:block;background:#7e8cff;border-radius:3px 3px 0 0}}.bars i:nth-child(2){{background:#45c7b6}}.bars i:nth-child(3){{background:#e2a85b}}.bars i:nth-child(4){{background:#e46b83}}.day small{{font-size:8px;color:#74819b}}.legend{{font-size:10px;color:#9daac4;margin-top:8px}}.legend span{{margin-right:12px}}.funnel-row{{margin:13px 0}}.funnel-row span{{display:inline-block;width:130px;font-size:12px}}.funnel-row b{{font-size:16px}}.funnel-bar{{height:8px;background:#26314a;border-radius:8px;margin-top:5px;overflow:hidden}}.funnel-bar i{{display:block;height:100%;background:#7e8cff;border-radius:8px}}details{{border:1px solid #293651;border-radius:10px;margin:7px 0;background:#10182a}}summary{{cursor:pointer;padding:11px;font-size:13px}}summary strong{{float:right;color:#8f9bb5;font-size:11px}}.children{{padding:0 14px 12px 28px}}.children div{{padding:7px 0;border-top:1px solid #222d45;font-size:12px}}.children span{{color:#8794af;margin-left:8px}}input,button{{border:1px solid #34415e;background:#0e1526;color:#edf2ff;border-radius:9px;padding:10px;font:inherit}}input{{width:100%;margin:5px 0}}button{{cursor:pointer;background:#27355a}}button:hover{{background:#344875}}.tool-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.result{{margin-top:8px;padding:10px;border-radius:9px;background:#0d1423;color:#aebcff;word-break:break-all}}.bar{{height:6px;background:#29344c;border-radius:5px}.bar span{{display:block;height:100%;background:#7e8cff;border-radius:5px}}a{{color:#9fb0ff}}@media(max-width:900px){{.grid{{grid-template-columns:repeat(2,1fr)}}.section-grid{{grid-template-columns:1fr}}}}@media(max-width:600px){{.container{{padding:14px}}.grid{{grid-template-columns:1fr}}.tool-grid{{grid-template-columns:1fr}}h1{{font-size:24px}}}}
</style></head><body><div class="container"><div class="header"><div><h1>⛏️ StoneDigger Command Center</h1><div class="sub">Sales • referrals • engagement • conversion • marketing</div></div><div><span class="pill">OXSHARE {'ACTIVE' if offer and offer['active'] else 'INACTIVE'}</span><div class="small">{offer_url}</div></div></div>
<div class="grid"><div class="card"><div class="small">TOTAL USERS</div><div class="kpi">{n(k['users'])}</div><div class="small">+{n(k['users_24h'])} in 24h</div></div><div class="card"><div class="small">OXSHARE CLICKS</div><div class="kpi">{n(k['clicks'])}</div><div class="small">+{n(k['clicks_24h'])} in 24h</div></div><div class="card"><div class="small">REFERRALS</div><div class="kpi">{n(k['referrals'])}</div><div class="small">Tracked invited users</div></div><div class="card"><div class="small">APPROVED CONVERSIONS</div><div class="kpi">{n(k['conversions'])}</div><div class="small">Revenue {money(k['revenue'])} • Commission {money(k['commission'])}</div></div></div>
<div class="section-grid"><div class="card"><h2>📈 Daily performance — last 30 days</h2><div class="chart">{chart_visual}</div><div class="legend"><span>▮ Users</span><span>▮ Clicks</span><span>▮ Referrals</span><span>▮ Conversions</span></div><div class="scroll"><table><thead><tr><th>Date</th><th>Users</th><th>Clicks</th><th>Referrals</th><th>Conversions</th><th>Revenue</th></tr></thead><tbody>{chart_rows}</tbody></table></div></div><div class="card"><h2>🔻 Conversion funnel</h2>{funnel_html}<hr style="border:0;border-top:1px solid #283550;margin:18px 0"><div class="small">Click → conversion: {(n(k['conversions'])/max(n(k['clicks']),1)*100):.1f}%</div><div class="small">Referral → user: {(n(k['referrals'])/max(n(k['users']),1)*100):.1f}%</div></div></div>
<div class="section-grid"><div class="card"><h2>📣 Marketing tools</h2><div class="tool-grid"><div><div class="small">REFERRER TELEGRAM ID</div><input id="rid" placeholder="e.g. 8929788738"><div class="small">CAMPAIGN NAME</div><input id="camp" placeholder="facebook_sept"><button onclick="makeLink()">Generate campaign link</button><div id="out" class="result">Your tracked Telegram campaign link will appear here.</div></div><div><div class="small">SHARE COPY</div><div class="result" id="copy">⛏️ Join StoneDigger FREE. Dig daily, build your streak and invite friends.\n\n{{LINK}}</div><button onclick="copyShare()">Copy share message</button><p class="small"><a href="/dashboard/export">Export all users CSV</a></p></div></div><div class="section" style="margin-top:18px"><h2>📊 Campaign performance</h2><div class="scroll"><table><thead><tr><th>Campaign</th><th>Clicks</th><th>Users</th></tr></thead><tbody>{campaign_rows or '<tr><td colspan=3 class=muted>No campaigns yet.</td></tr>'}</tbody></table></div></div></div><div class="card"><h2>🌳 Referral tree</h2>{tree_html}</div></div>
<div class="card"><h2>🏆 Top referrers</h2><div class="scroll"><table><thead><tr><th>#</th><th>User</th><th>Referrals</th><th>Activity</th><th>Streak</th></tr></thead><tbody>{top_rows}</tbody></table></div></div>
<div class="card"><h2>👥 Recent users</h2><div class="scroll"><table><thead><tr><th>User</th><th>Telegram ID</th><th>Activity</th><th>Streak</th><th>Referred</th><th>Created</th></tr></thead><tbody>{user_rows}</tbody></table></div></div>
<div class="card"><h2>🔗 Recent clicks</h2><div class="scroll"><table><thead><tr><th>Click ID</th><th>Telegram ID</th><th>Campaign</th><th>Offer</th><th>Created</th></tr></thead><tbody>{click_rows}</tbody></table></div></div>
<div class="card"><h2>💰 Recent conversions</h2><div class="scroll"><table><thead><tr><th>Click ID</th><th>Telegram ID</th><th>Provider</th><th>Amount</th><th>Commission</th><th>Status</th></tr></thead><tbody>{conversion_rows}</tbody></table></div></div>
</div><script>function makeLink(){{const id=document.getElementById('rid').value.trim();const c=document.getElementById('camp').value.trim().replace(/[^A-Za-z0-9_-]/g,'_');if(!id||!c){{document.getElementById('out').textContent='Enter both Telegram ID and campaign name.';return}}const link='https://t.me/{core.BOT_USERNAME}?start=ref_u'+id+'__c_'+c;document.getElementById('out').textContent=link;document.getElementById('copy').textContent='⛏️ Join StoneDigger FREE. Dig daily, build your streak and invite friends.\\n\\n'+link;}}function copyShare(){{navigator.clipboard.writeText(document.getElementById('copy').textContent);}}</script></body></html>'''
    return Response(page,mimetype="text/html")


# Replace the dashboard view and install the campaign-aware /start handler.
core.start = enhanced_start
core.app.view_functions["dashboard"] = enhanced_dashboard
core.app.add_url_rule("/dashboard/export", "dashboard_export", dashboard_export)
