import csv
import html
import io

from flask import Response, jsonify

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


async def campaign_start(update, context):
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
    await update.message.reply_text(core.welcome_text(user, stats), parse_mode="Markdown", reply_markup=core.main_keyboard(offer))


def export_users():
    auth = core.dashboard_auth()
    if auth:
        return auth
    with core.db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_user_id,username,first_name,activity_count,streak,referred_by_telegram_user_id,created_at,last_seen_at FROM public.users ORDER BY created_at DESC")
            rows = cur.fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["telegram_user_id","username","first_name","activity","streak","referred_by","created_at","last_seen_at"])
    for r in rows:
        w.writerow([r["telegram_user_id"],r["username"],r["first_name"],r["activity_count"],r["streak"],r["referred_by_telegram_user_id"],r["created_at"],r["last_seen_at"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment; filename=stonedigger-users.csv"})


def dashboard():
    auth = core.dashboard_auth()
    if auth:
        return auth
    try:
        with core.db() as conn:
            with conn.cursor() as c:
                c.execute("""SELECT
                  (SELECT COUNT(*) FROM public.users) users,
                  (SELECT COUNT(*) FROM public.users WHERE created_at >= now()-interval '24 hours') users_24h,
                  (SELECT COUNT(*) FROM public.clicks) clicks,
                  (SELECT COUNT(*) FROM public.clicks WHERE created_at >= now()-interval '24 hours') clicks_24h,
                  (SELECT COUNT(*) FROM public.users WHERE referred_by_telegram_user_id IS NOT NULL) referrals,
                  (SELECT COUNT(*) FROM public.conversions WHERE status IN ('approved','paid')) conversions,
                  (SELECT COALESCE(SUM(amount),0) FROM public.conversions WHERE status IN ('approved','paid')) revenue,
                  (SELECT COALESCE(SUM(commission),0) FROM public.conversions WHERE status IN ('approved','paid')) commission""")
                k = c.fetchone()
                c.execute("""SELECT COALESCE(u.username,u.first_name,'Miner') name,COUNT(r.telegram_user_id) referrals,COALESCE(u.activity_count,0) activity,COALESCE(u.streak,0) streak
                             FROM public.users u LEFT JOIN public.users r ON r.referred_by_telegram_user_id=u.telegram_user_id
                             GROUP BY u.telegram_user_id,u.username,u.first_name,u.activity_count,u.streak
                             ORDER BY referrals DESC,activity DESC LIMIT 20""")
                top = c.fetchall()
                c.execute("SELECT campaign_code,COUNT(*) clicks,COUNT(DISTINCT telegram_user_id) users FROM public.clicks GROUP BY campaign_code ORDER BY clicks DESC LIMIT 30")
                campaigns = c.fetchall()
                c.execute("""SELECT COALESCE(u.username,u.first_name,'Miner') referrer,COALESCE(r.username,r.first_name,'Miner') child,COALESCE(r.activity_count,0) activity,COALESCE(r.streak,0) streak
                             FROM public.users u JOIN public.users r ON r.referred_by_telegram_user_id=u.telegram_user_id ORDER BY u.created_at,r.created_at""")
                tree = c.fetchall()
                c.execute("SELECT COALESCE(username,first_name,'Miner') name,telegram_user_id,activity_count,streak,referred_by_telegram_user_id,created_at FROM public.users ORDER BY created_at DESC LIMIT 25")
                users = c.fetchall()
                c.execute("SELECT click_id,telegram_user_id,campaign_code,created_at FROM public.clicks ORDER BY created_at DESC LIMIT 25")
                clicks = c.fetchall()
    except Exception as exc:
        core.log.exception("Command Center query failed")
        return jsonify({"error":"dashboard_query_failed","detail":str(exc)}),500

    e=_e; n=_n
    groups={}
    for r in tree: groups.setdefault(r["referrer"],[]).append(r)
    tree_html="".join(f"<details><summary>👤 {e(name)} <b>{len(rows)} referral(s)</b></summary><div class='child'>"+"".join(f"<div>↳ {e(r['child'])} — ⛏️{n(r['activity'])} 🔥{n(r['streak'])}</div>" for r in rows)+"</div></details>" for name,rows in groups.items()) or "<p class='muted'>No referrals yet.</p>"
    top_html="".join(f"<tr><td>{i}</td><td>{e(r['name'])}</td><td>{n(r['referrals'])}</td><td>{n(r['activity'])}</td><td>{n(r['streak'])}</td></tr>" for i,r in enumerate(top,1))
    camp_html="".join(f"<tr><td><code>{e(r['campaign_code'])}</code></td><td>{n(r['clicks'])}</td><td>{n(r['users'])}</td></tr>" for r in campaigns) or "<tr><td colspan='3' class='muted'>No campaigns yet.</td></tr>"
    user_html="".join(f"<tr><td>{e(r['name'])}</td><td>{r['telegram_user_id']}</td><td>{n(r['activity_count'])}</td><td>{n(r['streak'])}</td><td>{'Yes' if r['referred_by_telegram_user_id'] else 'No'}</td><td>{e(r['created_at'])}</td></tr>" for r in users)
    click_html="".join(f"<tr><td><code>{e(r['click_id'])}</code></td><td>{r['telegram_user_id']}</td><td>{e(r['campaign_code'])}</td><td>{e(r['created_at'])}</td></tr>" for r in clicks)
    o=core.get_oxshare_offer(); offer_url=e(o["affiliate_url"]) if o else ""
    page=f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>StoneDigger Command Center</title><style>
body{{margin:0;background:#09101e;color:#edf2ff;font-family:system-ui}}.wrap{{max-width:1400px;margin:auto;padding:24px}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:#151e34;border:1px solid #2a3754;border-radius:15px;padding:16px;margin:12px 0}}.kpi{{font-size:30px;font-weight:800}}.muted{{color:#8c98b1}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border-bottom:1px solid #293650;text-align:left;white-space:nowrap}}.scroll{{overflow:auto}}code{{color:#b9c7ff}}details{{background:#10182a;border:1px solid #2b3855;border-radius:10px;margin:7px 0;padding:10px}}.child{{padding:8px 0 0 20px}}input,button{{padding:10px;border-radius:9px;border:1px solid #3a4868;background:#0e1628;color:#fff}}input{{width:100%;box-sizing:border-box;margin:5px 0}}button{{cursor:pointer}}.tool{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}a{{color:#aebcff}}@media(max-width:850px){{.grid,.tool{{grid-template-columns:1fr 1fr}}}}@media(max-width:600px){{.grid,.tool{{grid-template-columns:1fr}}}}</style></head><body><div class="wrap">
<h1>⛏️ StoneDigger Command Center</h1><p class="muted">Sales • referrals • engagement • campaigns • Oxshare only</p>
<div class="card"><b>OXSHARE ACTIVE</b><p class="muted">{offer_url}</p></div>
<div class="grid"><div class="card">USERS<div class="kpi">{n(k['users'])}</div><span class="muted">+{n(k['users_24h'])} in 24h</span></div><div class="card">OXSHARE CLICKS<div class="kpi">{n(k['clicks'])}</div><span class="muted">+{n(k['clicks_24h'])} in 24h</span></div><div class="card">REFERRALS<div class="kpi">{n(k['referrals'])}</div></div><div class="card">APPROVED CONVERSIONS<div class="kpi">{n(k['conversions'])}</div></div><div class="card">REVENUE<div class="kpi">{_money(k['revenue'])}</div></div><div class="card">COMMISSION<div class="kpi">{_money(k['commission'])}</div></div><div class="card">CLICK → CONVERSION<div class="kpi">{n(k['conversions'])/max(n(k['clicks']),1)*100:.1f}%</div></div><div class="card">REFERRAL → USER<div class="kpi">{n(k['referrals'])/max(n(k['users']),1)*100:.1f}%</div></div></div>
<div class="card"><h2>📣 Marketing Link Builder</h2><div class="tool"><div><label>Referrer Telegram ID</label><input id="rid" placeholder="e.g. 8929788738"><label>Campaign</label><input id="camp" placeholder="facebook_sept"><button onclick="makeLink()">Generate tracked link</button><div class="card" id="out">Your link will appear here.</div></div><div><b>Share message</b><div class="card" id="msg">⛏️ Join StoneDigger FREE. Dig daily, build your streak and invite friends.\n\n{{LINK}}</div><button onclick="copyMsg()">Copy message</button><p class="muted">Use separate campaign names for Facebook, Instagram and Telegram.</p><a href="/dashboard/export">Export users CSV</a></div></div></div>
<div class="card"><h2>📊 Campaign performance</h2><div class="scroll"><table><tr><th>Campaign</th><th>Clicks</th><th>Users</th></tr>{camp_html}</table></div></div>
<div class="card"><h2>🌳 Referral tree</h2>{tree_html}</div>
<div class="card"><h2>🏆 Top referrers</h2><div class="scroll"><table><tr><th>#</th><th>User</th><th>Referrals</th><th>Activity</th><th>Streak</th></tr>{top_html}</table></div></div>
<div class="card"><h2>👥 Recent users</h2><div class="scroll"><table><tr><th>User</th><th>ID</th><th>Activity</th><th>Streak</th><th>Referred</th><th>Created</th></tr>{user_html}</table></div></div>
<div class="card"><h2>🔗 Recent clicks</h2><div class="scroll"><table><tr><th>Click</th><th>User ID</th><th>Campaign</th><th>Created</th></tr>{click_html}</table></div></div>
</div><script>function makeLink(){{const id=document.getElementById('rid').value.trim();const c=document.getElementById('camp').value.trim().replace(/[^A-Za-z0-9_-]/g,'_');if(!id||!c){{document.getElementById('out').textContent='Enter both fields';return}}const l='https://t.me/{core.BOT_USERNAME}?start=ref_u'+id+'__c_'+c;document.getElementById('out').textContent=l;document.getElementById('msg').textContent='⛏️ Join StoneDigger FREE. Dig daily, build your streak and invite friends.\\n\\n'+l}}function copyMsg(){{navigator.clipboard.writeText(document.getElementById('msg').textContent)}}}</script></body></html>'''
    return Response(page,mimetype="text/html")


core.start = campaign_start
core.app.view_functions["dashboard"] = dashboard
if "dashboard_export" not in core.app.view_functions:
    core.app.add_url_rule("/dashboard/export", "dashboard_export", export_users)
