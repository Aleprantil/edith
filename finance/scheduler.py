"""
Notifiche automatiche pianificate (APScheduler)
"""

from datetime import date, timedelta
import database as db
from constants import fmt_amount, categoria_display


async def daily_summary(app):
    """Invia riepilogo giornaliero a tutti gli utenti alle 21:00"""
    users = db.get_all_users()
    today = str(date.today())
    for uid in users:
        settings = db.get_settings(uid)
        if not settings.get("notify_daily", 1):
            continue
        cur = settings.get("currency", "€")
        spese = db.get_category_totals(uid, today, today, "spesa")
        entrate = db.get_category_totals(uid, today, today, "entrata")
        tot_s = sum(r["total"] for r in spese)
        tot_e = sum(r["total"] for r in entrate)
        if tot_s == 0 and tot_e == 0:
            continue  # niente da riportare
        saldo = tot_e - tot_s
        emoji = "💰" if saldo >= 0 else "📉"
        lines = ["🌙 *Riepilogo serale di oggi*\n",
                 f"💚 Entrate:  {fmt_amount(tot_e, cur)}",
                 f"🔴 Spese:    {fmt_amount(tot_s, cur)}",
                 f"{emoji} Saldo:     {fmt_amount(saldo, cur)}"]
        if spese:
            lines.append("\n📂 Categorie:")
            for r in spese:
                lines.append(f"  {categoria_display(r['category'])} {fmt_amount(r['total'], cur)}")
        try:
            await app.bot.send_message(uid, "\n".join(lines), parse_mode="Markdown")
        except Exception:
            pass


async def weekly_summary(app):
    """Riepilogo settimanale domenica sera"""
    users = db.get_all_users()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    for uid in users:
        settings = db.get_settings(uid)
        if not settings.get("notify_weekly", 1):
            continue
        cur = settings.get("currency", "€")
        spese = db.get_category_totals(uid, str(week_start), str(today), "spesa")
        entrate = db.get_category_totals(uid, str(week_start), str(today), "entrata")
        tot_s = sum(r["total"] for r in spese)
        tot_e = sum(r["total"] for r in entrate)
        if tot_s == 0 and tot_e == 0:
            continue
        saldo = tot_e - tot_s
        msg = (
            f"📅 *Riepilogo settimanale*\n"
            f"Dal {week_start} al {today}\n\n"
            f"💚 Entrate:  {fmt_amount(tot_e, cur)}\n"
            f"🔴 Spese:    {fmt_amount(tot_s, cur)}\n"
            f"{'💰' if saldo>=0 else '😬'} Saldo:     {fmt_amount(saldo, cur)}\n\n"
            "Usa /settimana per i dettagli o /grafico per il grafico 📊"
        )
        try:
            await app.bot.send_message(uid, msg, parse_mode="Markdown")
        except Exception:
            pass


async def budget_check(app):
    """Controlla i budget a metà giornata e avvisa se vicini al limite"""
    users = db.get_all_users()
    today = date.today()
    month_start = today.replace(day=1) # CAMBIATO QUI
    for uid in users:
        settings = db.get_settings(uid)
        if not settings.get("notify_budget", 1):
            continue
        cur = settings.get("currency", "€")
        budgets = db.get_budgets(uid)
        if not budgets:
            continue
        spent_rows = db.get_category_totals(uid, str(month_start), str(today), "spesa")
        spent_d = {r["category"]: r["total"] for r in spent_rows}
        alerts = []
        for b in budgets:
            spent = spent_d.get(b["category"], 0)
            limit = b["monthly_limit"]
            pct = (spent / limit) * 100 if limit > 0 else 0
            if pct >= 100:
                alerts.append(f"🚨 *{categoria_display(b['category'])}*: SUPERATO! ({fmt_amount(spent, cur)}/{fmt_amount(limit, cur)})")
            elif pct >= 80:
                alerts.append(f"⚠️ *{categoria_display(b['category'])}*: {pct:.0f}% ({fmt_amount(spent, cur)}/{fmt_amount(limit, cur)})")
        if alerts:
            msg = "🔔 *Avviso Budget Mensile*\n\n" + "\n".join(alerts) + "\n\nUsa /budgetview per i dettagli."
            try:
                await app.bot.send_message(uid, msg, parse_mode="Markdown")
            except Exception:
                pass
