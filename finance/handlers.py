"""
Handler dei comandi Telegram
"""

import io
import csv
from datetime import date, timedelta
import os
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import database as db
from constants import (
    CATEGORIE_SPESA, TIPI_ENTRATA,
    fmt_amount, categoria_display, entrata_display
)
# --- EDITH: i grafici (matplotlib/numpy, pesanti in RAM) si caricano solo al primo uso ---
def _lazy_chart(name):
    def call(*args, **kwargs):
        import charts
        return getattr(charts, name)(*args, **kwargs)
    call.__name__ = name
    return call

pie_chart_categorie = _lazy_chart("pie_chart_categorie")
bar_chart_trend = _lazy_chart("bar_chart_trend")
bar_budget_comparison = _lazy_chart("bar_budget_comparison")
sparkline_saldo = _lazy_chart("sparkline_saldo")

# ── Stati conversazione (devono combaciare con bot.py) ──────────
SPESA_CAT, SPESA_IMP, SPESA_NOTA = 0, 1, 8
ENTRATA_TIPO, ENTRATA_IMP, ENTRATA_NOTA = 2, 3, 9
BUDGET_CAT, BUDGET_IMP = 4, 5

# ── Controllo accesso Multi-Utente ─────────────────────────────

def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # Leggiamo la stringa dal .env (es: "123456,876543")
        raw_ids = os.environ.get("ADMIN_IDS", "")
        
        # Trasformiamo la stringa in una lista di numeri interi
        # Questo comando pulisce eventuali spazi e crea la lista [123456, 876543]
        allowed_ids = [int(i.strip()) for i in raw_ids.split(",") if i.strip()]

        user_id = update.effective_user.id
        
        # Se la lista non è vuota e l'utente NON è nella lista, lo blocca
        if allowed_ids and user_id not in allowed_ids:
            print(f"⚠️ Accesso negato per l'utente non autorizzato: {user_id}")
            return
            
        return await func(update, context, *args, **kwargs)
    return wrapped

# ═══════════════════════════════════════════════════════════════
#  START / HELP
# ═══════════════════════════════════════════════════════════════

@restricted
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db.get_settings(uid)
    name = update.effective_user.first_name
    text = (
        f"👋 Ciao *{name}*! Sono il tuo assistente finanziario personale.\n\n"
        "💸 *Spese & Entrate*\n"
        "  /spesa — registra una spesa\n"
        "  /entrata — registra un'entrata\n"
        "  /elimina — cancella l'ultima transazione\n\n"
        "📊 *Resoconti*\n"
        "  /oggi — riepilogo di oggi\n"
        "  /settimana — questa settimana\n"
        "  /mese — questo mese\n"
        "  /anno — quest'anno\n\n"
        "📈 *Grafici*\n"
        "  /grafico — torta categorie (mese)\n"
        "  /trend — andamento spese giornaliere\n\n"
        "🎯 *Budget & Obiettivi*\n"
        "  /budget — imposta budget mensile per categoria\n"
        "  /budgetview — visualizza i budget impostati\n"
        "  /obiettivi — gestisci obiettivi di risparmio\n\n"
        "🔍 *Altro*\n"
        "  /cerca [parola] — cerca transazioni\n"
        "  /esporta — esporta CSV del mese\n"
        "  /aiuto — mostra questo messaggio\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

@restricted
async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

@restricted
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operazione annullata.")
    return -1


# ═══════════════════════════════════════════════════════════════
#  SPESA
# ═══════════════════════════════════════════════════════════════

@restricted
async def spesa_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flat = [(label, code) for label, code in CATEGORIE_SPESA.items()]
    rows = []
    for i in range(0, len(flat), 2):
        row = [InlineKeyboardButton(flat[i][0], callback_data=f"cat_{flat[i][1]}")]
        if i + 1 < len(flat):
            row.append(InlineKeyboardButton(flat[i+1][0], callback_data=f"cat_{flat[i+1][1]}"))
        rows.append(row)
    markup = InlineKeyboardMarkup(rows)
    await update.message.reply_text("📂 *Seleziona la categoria della spesa:*", reply_markup=markup, parse_mode="Markdown")
    return SPESA_CAT

@restricted
async def spesa_categoria(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.replace("cat_", "")
    ctx.user_data["spesa_cat"] = code
    display = categoria_display(code)
    await query.edit_message_text(f"📂 Categoria: *{display}*\n\n💶 Quanto hai speso? (es. `12.50`)", parse_mode="Markdown")
    return SPESA_IMP

@restricted
async def spesa_importo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Inserisci un numero valido (es. `12.50`)", parse_mode="Markdown")
        return SPESA_IMP

    ctx.user_data["spesa_amount"] = amount
    cat = ctx.user_data.get("spesa_cat", "altro")
    display = categoria_display(cat)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Salta nota", callback_data="nota_skip_spesa")]])
    await update.message.reply_text(
        f"📂 *{display}* — {fmt_amount(amount)}\n\n"
        "📝 Aggiungi una nota opzionale (es. `McDonald's`, `Esselunga`)\n"
        "oppure premi *Salta nota*.",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    return SPESA_NOTA

@restricted
async def spesa_nota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    nota = update.message.text.strip()
    await _salva_spesa(uid, nota, ctx, update.message.reply_text)
    return -1

@restricted
async def spesa_nota_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    await _salva_spesa(uid, "", ctx, query.message.reply_text)
    return -1

# ── funzioni interne: NON hanno @restricted (non sono handler Telegram) ──

async def _salva_spesa(uid, nota, ctx, reply_fn):
    amount = ctx.user_data.get("spesa_amount", 0)
    cat = ctx.user_data.get("spesa_cat", "altro")
    db.add_transaction(uid, "spesa", cat, amount, nota)
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    display = categoria_display(cat)
    budget_msg = await _check_budget_alert(uid, cat, cur)
    nota_str = f"\n📝 _{nota}_" if nota else ""
    reply = f"✅ Spesa registrata!\n*{display}* — {fmt_amount(amount, cur)}{nota_str}"
    if budget_msg:
        reply += f"\n\n{budget_msg}"
    await reply_fn(reply, parse_mode="Markdown")

async def _check_budget_alert(uid, category, currency="€"):
    budget = db.get_budget(uid, category)
    if not budget:
        return None
    limit = budget["monthly_limit"] # Usiamo il nuovo nome colonna
    today = date.today()
    month_start = today.replace(day=1) # LOGICA MENSILE
    rows = db.get_category_totals(uid, str(month_start), str(today))
    spent = next((r["total"] for r in rows if r["category"] == category), 0)
    pct = (spent / limit) * 100 if limit > 0 else 0
    display = categoria_display(category)
    if pct >= 100:
        return f"🚨 *BUDGET MENSILE SUPERATO* per {display}!\nSpeso {fmt_amount(spent, currency)} su {fmt_amount(limit, currency)} questo mese."
    elif pct >= 80:
        return f"⚠️ *Attenzione budget* {display}: {pct:.0f}% usato ({fmt_amount(spent, currency)}/{fmt_amount(limit, currency)})"
    return None

# ═══════════════════════════════════════════════════════════════
#  ENTRATA
# ═══════════════════════════════════════════════════════════════

@restricted
async def entrata_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flat = list(TIPI_ENTRATA.items())
    rows = []
    for i in range(0, len(flat), 2):
        row = [InlineKeyboardButton(flat[i][0], callback_data=f"inc_{flat[i][1]}")]
        if i + 1 < len(flat):
            row.append(InlineKeyboardButton(flat[i+1][0], callback_data=f"inc_{flat[i+1][1]}"))
        rows.append(row)
    markup = InlineKeyboardMarkup(rows)
    await update.message.reply_text("💰 *Che tipo di entrata?*", reply_markup=markup, parse_mode="Markdown")
    return ENTRATA_TIPO

@restricted
async def entrata_tipo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.replace("inc_", "")
    ctx.user_data["entrata_tipo"] = code
    display = entrata_display(code)
    await query.edit_message_text(f"💰 Tipo: *{display}*\n\n💶 Quanto hai ricevuto? (es. `1500`)", parse_mode="Markdown")
    return ENTRATA_IMP

@restricted
async def entrata_importo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Inserisci un numero valido", parse_mode="Markdown")
        return ENTRATA_IMP

    ctx.user_data["entrata_amount"] = amount
    tipo = ctx.user_data.get("entrata_tipo", "altro_entrata")
    display = entrata_display(tipo)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Salta nota", callback_data="nota_skip_entrata")]])
    await update.message.reply_text(
        f"💰 *{display}* — {fmt_amount(amount)}\n\n"
        "📝 Aggiungi una nota opzionale (es. `Cliente Rossi`, `Bonus`)\n"
        "oppure premi *Salta nota*.",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    return ENTRATA_NOTA

@restricted
async def entrata_nota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    nota = update.message.text.strip()
    await _salva_entrata(uid, nota, ctx, update.message.reply_text)
    return -1

@restricted
async def entrata_nota_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    await _salva_entrata(uid, "", ctx, query.message.reply_text)
    return -1

async def _salva_entrata(uid, nota, ctx, reply_fn):
    amount = ctx.user_data.get("entrata_amount", 0)
    tipo = ctx.user_data.get("entrata_tipo", "altro_entrata")
    db.add_transaction(uid, "entrata", tipo, amount, nota)
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    display = entrata_display(tipo)
    nota_str = f"\n📝 _{nota}_" if nota else ""
    await reply_fn(
        f"✅ Entrata registrata!\n*{display}* — {fmt_amount(amount, cur)}{nota_str} 🎉",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════
#  RESOCONTI
# ═══════════════════════════════════════════════════════════════

def _progress_bar(value, total, length=8):
    if total == 0:
        return "░" * length
    filled = int(round((value / total) * length))
    return "█" * filled + "░" * (length - filled)

async def _riepilogo(update, uid, from_date, to_date, titolo):
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    spese_cat = db.get_category_totals(uid, from_date, to_date, "spesa")
    entrate_cat = db.get_category_totals(uid, from_date, to_date, "entrata")
    tot_spese = sum(r["total"] for r in spese_cat)
    tot_entrate = sum(r["total"] for r in entrate_cat)
    saldo = tot_entrate - tot_spese
    lines = [f"📊 *{titolo}*\n"]
    lines.append(f"💚 Entrate:  {fmt_amount(tot_entrate, cur)}")
    lines.append(f"🔴 Spese:    {fmt_amount(tot_spese, cur)}")
    emoji_saldo = "💰" if saldo >= 0 else "😬"
    lines.append(f"{emoji_saldo} Saldo:     {fmt_amount(saldo, cur)}\n")
    if spese_cat:
        lines.append("📂 *Spese per categoria:*")
        for r in spese_cat:
            bar = _progress_bar(r["total"], tot_spese)
            lines.append(f"  {categoria_display(r['category'])} {bar} {fmt_amount(r['total'], cur)}")
    if entrate_cat:
        lines.append("\n💰 *Fonti entrate:*")
        for r in entrate_cat:
            lines.append(f"  {entrata_display(r['category'])} {fmt_amount(r['total'], cur)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@restricted
async def riepilogo_giornaliero(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = str(date.today())
    await _riepilogo(update, update.effective_user.id, today, today, f"Riepilogo di oggi — {today}")

@restricted
async def riepilogo_settimanale(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    await _riepilogo(update, update.effective_user.id, str(week_start), str(today), "Riepilogo settimana corrente")

@restricted
async def riepilogo_mensile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    month_start = today.replace(day=1)
    await _riepilogo(update, update.effective_user.id, str(month_start), str(today), f"Riepilogo {today.strftime('%B %Y')}")

@restricted
async def riepilogo_annuale(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    year_start = today.replace(month=1, day=1)
    await _riepilogo(update, update.effective_user.id, str(year_start), str(today), f"Riepilogo {today.year}")


# ═══════════════════════════════════════════════════════════════
#  GRAFICI
# ═══════════════════════════════════════════════════════════════

@restricted
async def grafico_categorie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    today = date.today()
    month_start = today.replace(day=1)
    rows = db.get_category_totals(uid, str(month_start), str(today))
    if not rows:
        await update.message.reply_text("📊 Nessuna spesa questo mese.")
        return
    buf = pie_chart_categorie(rows, f"Spese per categoria — {today.strftime('%B %Y')}")
    await update.message.reply_photo(buf, caption=f"🍕 Distribuzione spese {today.strftime('%B %Y')}")

@restricted
async def grafico_trend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    today = date.today()
    month_start = today.replace(day=1)
    spese = db.get_daily_totals(uid, str(month_start), str(today), "spesa")
    entrate = db.get_daily_totals(uid, str(month_start), str(today), "entrata")
    if not spese and not entrate:
        await update.message.reply_text("📈 Nessun dato questo mese.")
        return
    buf = bar_chart_trend(spese, entrate, f"Trend giornaliero — {today.strftime('%B %Y')}")
    await update.message.reply_photo(buf, caption="📈 Andamento spese ed entrate")


# ═══════════════════════════════════════════════════════════════
#  BUDGET
# ═══════════════════════════════════════════════════════════════

@restricted
async def budget_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flat = list(CATEGORIE_SPESA.items())
    rows = []
    for i in range(0, len(flat), 2):
        row = [InlineKeyboardButton(flat[i][0], callback_data=f"bcat_{flat[i][1]}")]
        if i + 1 < len(flat):
            row.append(InlineKeyboardButton(flat[i+1][0], callback_data=f"bcat_{flat[i+1][1]}"))
        rows.append(row)
    markup = InlineKeyboardMarkup(rows)
    await update.message.reply_text("🎯 *Imposta budget mensile*\n\nSeleziona la categoria:", reply_markup=markup, parse_mode="Markdown")
    return BUDGET_CAT

@restricted
async def budget_categoria(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.replace("bcat_", "")
    ctx.user_data["budget_cat"] = code
    display = categoria_display(code)
    await query.edit_message_text(f"🎯 Budget per *{display}*\n\n💶 Inserisci il limite mensile:", parse_mode="Markdown")
    return BUDGET_IMP

@restricted
async def budget_importo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Inserisci un numero valido")
        return BUDGET_IMP
    cat = ctx.user_data.get("budget_cat", "altro")
    db.set_budget(uid, cat, amount)
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    display = categoria_display(cat)
    await update.message.reply_text(
        f"✅ Budget impostato!\n*{display}*: {fmt_amount(amount, cur)}/mese",
        parse_mode="Markdown"
    )
    return -1

@restricted
async def budget_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    budgets = db.get_budgets(uid)
    if not budgets:
        await update.message.reply_text("📋 Nessun budget impostato.\nUsa /budget per iniziare!")
        return
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    today = date.today()
    month_start = today.replace(day=1) # LOGICA MENSILE
    lines = ["📋 *Budget mensili:*\n"]
    for b in budgets:
        rows = db.get_category_totals(uid, str(month_start), str(today))
        spent = next((r["total"] for r in rows if r["category"] == b["category"]), 0)
        limit = b["monthly_limit"]
        pct = (spent / limit) * 100 if limit > 0 else 0
        bar = _progress_bar(spent, limit)
        status = "🟢" if pct < 60 else ("🟡" if pct < 85 else ("🔴" if pct < 100 else "🚨"))
        display = categoria_display(b["category"])
        lines.append(f"{status} *{display}*")
        lines.append(f"   {bar} {pct:.0f}% — {fmt_amount(spent, cur)}/{fmt_amount(limit, cur)}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    
    # Grafico
    rows_all = db.get_category_totals(uid, str(month_start), str(today))
    buf = bar_budget_comparison(budgets, rows_all, cur)
    if buf:
        await update.message.reply_photo(buf, caption="📊 Budget vs Spese questo mese")

async def budget_alert_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass  # usato dallo scheduler


# ═══════════════════════════════════════════════════════════════
#  CERCA / ELIMINA / ESPORTA
# ═══════════════════════════════════════════════════════════════

@restricted
async def cerca_transazione(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = ctx.args
    if not args:
        await update.message.reply_text("🔍 Uso: /cerca [parola]\nEs: /cerca pizza")
        return
    keyword = " ".join(args)
    results = db.search_transactions(uid, keyword)
    if not results:
        await update.message.reply_text(f"🔍 Nessun risultato per *{keyword}*", parse_mode="Markdown")
        return
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    lines = [f"🔍 *Risultati per «{keyword}»:*\n"]
    for r in results[:10]:
        tipo_ico = "🔴" if r["type"] == "spesa" else "💚"
        cat = categoria_display(r["category"]) if r["type"] == "spesa" else entrata_display(r["category"])
        nota_str = f" — _{r['note']}_" if r["note"] else ""
        lines.append(f"{tipo_ico} {cat} — {fmt_amount(r['amount'], cur)}{nota_str}\n   📅 {r['created_at'][:10]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@restricted
async def elimina_ultima(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    last = db.get_last_transaction(uid)
    if not last:
        await update.message.reply_text("🗑️ Nessuna transazione da eliminare.")
        return
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    if last["type"] == "spesa":
        cat = categoria_display(last["category"])
        tipo_ico = "🔴"
    else:
        cat = entrata_display(last["category"])
        tipo_ico = "💚"
    db.delete_transaction(last["id"])
    await update.message.reply_text(
        f"🗑️ Eliminata l'ultima transazione:\n{tipo_ico} {cat} — {fmt_amount(last['amount'], cur)}\n📅 {last['created_at'][:10]}",
        parse_mode="Markdown"
    )

@restricted
async def esporta_csv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    today = date.today()
    month_start = today.replace(day=1)
    rows = db.get_transactions(uid, str(month_start), str(today))
    if not rows:
        await update.message.reply_text("📄 Nessuna transazione da esportare.")
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Data", "Tipo", "Categoria", "Importo", "Note"])
    for r in rows:
        writer.writerow([r["created_at"][:10], r["type"], r["category"], r["amount"], r["note"] or ""])
    output.seek(0)
    filename = f"finanze_{today.strftime('%Y_%m')}.csv"
    await update.message.reply_document(
        document=io.BytesIO(output.getvalue().encode("utf-8-sig")),
        filename=filename,
        caption=f"📄 Esportazione {today.strftime('%B %Y')}"
    )


# ═══════════════════════════════════════════════════════════════
#  OBIETTIVI DI RISPARMIO
# ═══════════════════════════════════════════════════════════════

@restricted
async def obiettivi_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    goals = db.get_goals(uid)
    settings = db.get_settings(uid)
    cur = settings.get("currency", "€")
    lines = ["🎯 *Obiettivi di risparmio:*\n"]
    if not goals:
        lines.append("Nessun obiettivo impostato.")
    else:
        for g in goals:
            pct = (g["saved"] / g["target"]) * 100 if g["target"] > 0 else 0
            bar = _progress_bar(g["saved"], g["target"], 10)
            lines.append(f"🎯 *{g['name']}*")
            lines.append(f"   {bar} {pct:.0f}%")
            lines.append(f"   {fmt_amount(g['saved'], cur)} / {fmt_amount(g['target'], cur)}\n")
    buttons = [
        [InlineKeyboardButton("➕ Nuovo obiettivo", callback_data="obj_new")],
        [InlineKeyboardButton("➕ Aggiungi risparmio", callback_data="obj_add")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("\n".join(lines), reply_markup=markup, parse_mode="Markdown")

@restricted
async def obiettivi_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    if action == "obj_new":
        await query.edit_message_text("🎯 Invia il nome del nuovo obiettivo (es. `Vacanza Sardegna`):", parse_mode="Markdown")
        ctx.user_data["obj_step"] = "name"
    elif action == "obj_add":
        await query.edit_message_text("💰 Invia `[id_obiettivo] [importo]` da aggiungere:")
        ctx.user_data["obj_step"] = "add"

@restricted
async def obiettivi_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gestisce l'inserimento di testo per gli obiettivi"""
    step = ctx.user_data.get("obj_step")
    uid = update.effective_user.id
    text = update.message.text.strip()

    if step == "name":
        ctx.user_data["new_obj_name"] = text
        await update.message.reply_text(f"🎯 Obiettivo: *{text}*\n\n💶 Qual è la cifra totale da raggiungere?", parse_mode="Markdown")
        ctx.user_data["obj_step"] = "target"
        
    elif step == "target":
        try:
            target = float(text.replace(",", "."))
            name = ctx.user_data.get("new_obj_name")
            db.add_goal(uid, name, target)
            await update.message.reply_text(f"✅ Obiettivo *{name}* creato! Bersaglio: {target}€")
            ctx.user_data["obj_step"] = None
        except ValueError:
            await update.message.reply_text("⚠️ Inserisci un numero valido.")

    elif step == "add":
        try:
            # L'utente scrive "1 50" -> id 1, importo 50
            parts = text.split()
            goal_id = int(parts[0])
            amount = float(parts[1].replace(",", "."))
            db.update_goal(goal_id, amount)
            await update.message.reply_text(f"💰 Versamento di {amount}€ effettuato con successo!")
            ctx.user_data["obj_step"] = None
        except (ValueError, IndexError):
            await update.message.reply_text("⚠️ Formato errato. Usa: `[id_obiettivo] [importo]` (es: `1 50`)")