"""
Finanze in linguaggio naturale ("ho speso 12 euro di pizza", "quanto ho speso questo mese?").

Usa i moduli del FinanceBot così come sono (database, constants, handlers): per usare
questo file serve che config.py abbia già aggiunto la cartella finance/ al percorso di import.
Ogni funzione restituisce il testo da mandare all'utente (Markdown).
"""
import logging
import math
from datetime import date, datetime, timedelta

import database as db
from constants import CATEGORIE_SPESA, TIPI_ENTRATA, categoria_display, entrata_display, fmt_amount
from handlers import _check_budget_alert, _progress_bar

logger = logging.getLogger(__name__)

# codice minuscolo -> codice reale nel database (es. "ristoranti" -> "Ristoranti")
SPESA_CODES = {c.lower(): c for c in CATEGORIE_SPESA.values()}
ENTRATA_CODES = {c.lower(): c for c in TIPI_ENTRATA.values()}

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

# Periodi che il codice calcola da solo (niente date inventate da Gemini). Altri: "personalizzato" + date.
PERIODS = ("oggi", "ieri", "settimana", "settimana_scorsa", "mese", "mese_scorso", "anno", "anno_scorso", "personalizzato")


def parse_amount(value) -> float | None:
    """12, 12.5, "12,50", "1.234,50", "€ 12" -> float positivo; altrimenti None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        s = str(value).replace("€", "").replace("euro", "").replace(" ", "").strip()
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")
        try:
            amount = float(s)
        except ValueError:
            return None
    return round(amount, 2) if math.isfinite(amount) and amount > 0 else None


def _past_timestamp(start_time_str) -> str | None:
    """Data/ora della transazione, solo se l'utente ha indicato un giorno PASSATO ("ieri", "il 3")."""
    if not start_time_str:
        return None
    try:
        dt = datetime.fromisoformat(str(start_time_str)).replace(tzinfo=None)
    except ValueError:
        return None
    if dt.date() >= date.today():   # oggi (default del database) o futuro: ignoro
        return None
    if dt.hour == 0 and dt.minute == 0:
        dt = dt.replace(hour=12)    # giorno senza orario: mezzogiorno
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _currency(uid) -> str:
    return db.get_settings(uid).get("currency", "€")


async def add_expense(uid: int, amount_raw, category_raw, note: str, start_time_str) -> str:
    amount = parse_amount(amount_raw)
    if amount is None:
        return "❌ Non ho capito l'importo. Prova con: «ho speso 12,50 di pizza»."

    cat = SPESA_CODES.get(str(category_raw or "").strip().lower(), "altro")
    ts = _past_timestamp(start_time_str)
    note = (note or "").strip()

    db.add_transaction(uid, "spesa", cat, amount, note, created_at=ts)
    cur = _currency(uid)

    text = f"✅ Spesa registrata!\n*{categoria_display(cat)}* — {fmt_amount(amount, cur)}"
    if note:
        text += f"\n📝 _{note}_"
    if ts:
        text += f"\n📅 {ts[:10]}"
    # L'avviso budget ha senso solo per il mese corrente
    if not ts or ts[:7] == date.today().strftime("%Y-%m"):
        alert = await _check_budget_alert(uid, cat, cur)
        if alert:
            text += f"\n\n{alert}"
    return text


async def add_income(uid: int, amount_raw, category_raw, note: str, start_time_str) -> str:
    amount = parse_amount(amount_raw)
    if amount is None:
        return "❌ Non ho capito l'importo. Prova con: «ho ricevuto lo stipendio 1500»."

    tipo = ENTRATA_CODES.get(str(category_raw or "").strip().lower(), "altro_entrata")
    ts = _past_timestamp(start_time_str)
    note = (note or "").strip()

    db.add_transaction(uid, "entrata", tipo, amount, note, created_at=ts)
    cur = _currency(uid)

    text = f"✅ Entrata registrata!\n*{entrata_display(tipo)}* — {fmt_amount(amount, cur)}"
    if note:
        text += f"\n📝 _{note}_"
    if ts:
        text += f"\n📅 {ts[:10]}"
    return text + " 🎉"


def _parse_day(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _period_range(period: str, start_raw=None, end_raw=None):
    """(da, a, titolo) per il periodo richiesto. Date comprese. Sconosciuto -> mese corrente."""
    today = date.today()
    if period == "personalizzato":
        a, b = _parse_day(start_raw), _parse_day(end_raw) or _parse_day(start_raw)
        if a:
            b = b or a
            if b < a:
                a, b = b, a
            if a == b:
                return a, b, f"Riepilogo del {a.strftime('%d/%m/%Y')}"
            return a, b, f"Riepilogo dal {a.strftime('%d/%m/%Y')} al {b.strftime('%d/%m/%Y')}"
        period = "mese"   # date non valide: ripiego sul mese corrente

    if period == "oggi":
        return today, today, f"Riepilogo di oggi — {today.strftime('%d/%m/%Y')}"
    if period == "ieri":
        y = today - timedelta(days=1)
        return y, y, f"Riepilogo di ieri — {y.strftime('%d/%m/%Y')}"
    if period == "settimana":
        return today - timedelta(days=today.weekday()), today, "Riepilogo settimana corrente"
    if period == "settimana_scorsa":
        mon = today - timedelta(days=today.weekday() + 7)
        return mon, mon + timedelta(days=6), "Riepilogo settimana scorsa"
    if period == "mese_scorso":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last, f"Riepilogo {MESI[last.month - 1]} {last.year}"
    if period == "anno":
        return today.replace(month=1, day=1), today, f"Riepilogo {today.year}"
    if period == "anno_scorso":
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31), f"Riepilogo {y}"
    return today.replace(day=1), today, f"Riepilogo {MESI[today.month - 1]} {today.year}"


async def get_summary(uid: int, period_raw, category_raw=None, start_raw=None, end_raw=None) -> str:
    period = str(period_raw or "mese").strip().lower().replace(" ", "_")
    if period not in PERIODS:
        period = "mese"
    start, end, title = _period_range(period, start_raw, end_raw)
    cur = _currency(uid)

    # Domanda su una categoria precisa ("quanto ho speso in ristoranti quest'anno?")
    key = str(category_raw or "").strip().lower()
    if key and key not in ("altro", "altro_entrata"):
        if key in SPESA_CODES:
            code, kind, label = SPESA_CODES[key], "spesa", categoria_display(SPESA_CODES[key])
        elif key in ENTRATA_CODES:
            code, kind, label = ENTRATA_CODES[key], "entrata", entrata_display(ENTRATA_CODES[key])
        else:
            code = None
        if code:
            rows = db.get_category_totals(uid, str(start), str(end), kind)
            total = next((r["total"] for r in rows if r["category"] == code), 0)
            verbo = "Speso" if kind == "spesa" else "Incassato"
            periodo = title.replace("Riepilogo ", "", 1)
            periodo = periodo[3:] if periodo.startswith("di ") else periodo
            return f"{label} · {periodo}\n{verbo}: *{fmt_amount(total, cur)}*"

    spese_cat = db.get_category_totals(uid, str(start), str(end), "spesa")
    entrate_cat = db.get_category_totals(uid, str(start), str(end), "entrata")
    tot_spese = sum(r["total"] for r in spese_cat)
    tot_entrate = sum(r["total"] for r in entrate_cat)
    saldo = tot_entrate - tot_spese

    lines = [f"📊 *{title}*\n",
             f"💚 Entrate:  {fmt_amount(tot_entrate, cur)}",
             f"🔴 Spese:    {fmt_amount(tot_spese, cur)}",
             f"{'💰' if saldo >= 0 else '😬'} Saldo:     {fmt_amount(saldo, cur)}\n"]
    if spese_cat:
        lines.append("📂 *Spese per categoria:*")
        for r in spese_cat:
            lines.append(f"  {categoria_display(r['category'])} {_progress_bar(r['total'], tot_spese)} {fmt_amount(r['total'], cur)}")
    if entrate_cat:
        lines.append("\n💰 *Fonti entrate:*")
        for r in entrate_cat:
            lines.append(f"  {entrata_display(r['category'])} {fmt_amount(r['total'], cur)}")
    if not spese_cat and not entrate_cat:
        lines.append("Nessuna transazione in questo periodo.")
    return "\n".join(lines)
