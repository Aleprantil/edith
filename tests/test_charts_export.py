"""Grafici salvati in Nextcloud: cartelle, sovrascrittura quotidiana, copia di fine mese, recupero del mese scorso, errori comprensibili."""
import io
import sys
import types
from datetime import datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest

from services import charts_export as CE
from services import notes_service as ns

TZ = ZoneInfo("Europe/Rome")
PNG = b"\x89PNG\r\n\x1a\n"
UID = 777


class Res:
    def __init__(self, code):
        self.status_code = code


class FakeDAV:
    """Un Nextcloud finto: cartelle e file, con le risposte vere (201 creato, 204 sostituito, 405 cartella già esistente, 409 manca la cartella)."""
    def __init__(self, missing_parent=409):
        self.missing_parent = missing_parent                  # come risponde questo Nextcloud se manca la cartella: 409 (standard) o 404
        self.base = ns.get_webdav_base_url()
        self.folders, self.files, self.log, self.fail, self.urls = set(), {}, [], {}, []

    def req(self, method, url, **kw):
        assert url.startswith(self.base + "/"), url
        path = unquote(url[len(self.base) + 1:]).strip("/")
        self.log.append((method, path))
        self.urls.append(url)
        if method in self.fail:
            return Res(self.fail[method])
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if method == "MKCOL":
            if parent and parent not in self.folders:
                return Res(409)
            if path in self.folders:
                return Res(405)
            self.folders.add(path)
            return Res(201)
        if method == "PUT":
            if parent and parent not in self.folders:
                return Res(self.missing_parent)
            new = path not in self.files
            self.files[path] = kw["data"]
            assert kw["headers"]["Content-Type"] == "image/png"
            return Res(201 if new else 204)
        if method == "PROPFIND":
            return Res(207 if path in self.files or path in self.folders else 404)
        raise AssertionError(method)

    def puts(self):
        return [p for m, p in self.log if m == "PUT"]


@pytest.fixture
def dav(monkeypatch):
    d = FakeDAV()
    monkeypatch.setattr(ns, "_req", d.req)
    return d


@pytest.fixture
def fin(monkeypatch):
    """Spese di ottobre 2026 e di settembre nel database di prova, più il finto modulo dei grafici (restituisce PNG riconoscibili)."""
    import database as db
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE user_id=?", (UID,))
        conn.execute("DELETE FROM budgets WHERE user_id=?", (UID,))
    for d, cat, amt in (("2026-10-01", "ristoranti", 18.0), ("2026-10-02", "spesa", 30.0), ("2026-09-03", "trasporti", 40.0), ("2026-09-20", "ristoranti", 25.0)):
        db.add_transaction(UID, "spesa", cat, amt, "x", f"{d} 12:00:00")
    db.set_budget(UID, "ristoranti", 150.0)
    calls = {"pie": [], "bar": [], "rows": []}
    fake = types.ModuleType("charts")
    fake.pie_chart_categorie = lambda rows, title="": (calls["pie"].append((len(rows), title)), calls["rows"].append(sum(r["total"] for r in rows)), io.BytesIO(PNG + b"pie" + title.encode()))[2]
    fake.bar_budget_comparison = lambda budgets, rows, currency="€": (calls["bar"].append(len(budgets)), PNG + b"bar")[1]
    monkeypatch.setitem(sys.modules, "charts", fake)
    return calls


def at(y, m, d, h=23, mi=55):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Il lavoro di ogni notte
# ---------------------------------------------------------------------------

def test_a_normal_day_updates_the_two_files_and_recovers_last_months_copy(dav, fin):
    rep = CE.run(UID, at(2026, 10, 6))
    assert rep["updated"] == ["grafico_spese.png", "budget.png"]
    assert dav.files["Grafici EDITH/grafico_spese.png"].startswith(PNG + b"pie") and b"ottobre 2026" in dav.files["Grafici EDITH/grafico_spese.png"]
    assert b"06/10 23:55" in dav.files["Grafici EDITH/grafico_spese.png"]
    assert dav.files["Grafici EDITH/budget.png"] == PNG + b"bar"
    assert not any("2026-10" in p for p in dav.files)                                   # a metà mese nessuna copia del mese in corso
    assert rep["archived"] == ["2026-09 grafico_spese.png", "2026-09 budget.png"]       # ma il mese scorso, che mancava, sì
    assert b"settembre 2026 (finale)" in dav.files["Grafici EDITH/Archivio mensile/2026-09 grafico_spese.png"]
    assert fin["pie"] == [(2, "Spese ottobre 2026 (aggiornato 06/10 23:55)"), (2, "Spese settembre 2026 (finale)")] and fin["bar"] == [1, 1]
    assert {"Grafici EDITH", "Grafici EDITH/Archivio mensile"} <= dav.folders


def test_the_next_day_replaces_the_files_and_does_not_touch_the_archive(dav, fin):
    CE.run(UID, at(2026, 10, 6))
    archive_before = {k: v for k, v in dav.files.items() if "Archivio" in k}
    dav.log.clear()
    rep = CE.run(UID, at(2026, 10, 7))
    assert rep["archived"] == [] and {k: v for k, v in dav.files.items() if "Archivio" in k} == archive_before            # già c'era: non la rifà
    assert [p for p in dav.puts()] == ["Grafici EDITH/grafico_spese.png", "Grafici EDITH/budget.png"]                      # solo i due del giorno
    assert b"07/10 23:55" in dav.files["Grafici EDITH/grafico_spese.png"]                                                  # il vecchio è stato sostituito
    assert sum(1 for p in dav.files if p.startswith("Grafici EDITH/") and "Archivio" not in p) == 2                        # sempre due file, non si accumulano
    assert not [m for m, _ in dav.log if m == "MKCOL"]                                                                     # niente da creare


def test_the_last_day_of_the_month_keeps_a_copy_that_is_never_touched_again(dav, fin):
    import database as db
    db.add_transaction(UID, "spesa", "spesa", 12.0, "x", "2026-10-31 18:00:00")
    CE.run(UID, at(2026, 10, 30))
    assert "Grafici EDITH/Archivio mensile/2026-10 grafico_spese.png" not in dav.files
    rep = CE.run(UID, at(2026, 10, 31))
    assert rep["archived"] == ["2026-10 grafico_spese.png", "2026-10 budget.png"]
    kept = dict(dav.files)["Grafici EDITH/Archivio mensile/2026-10 grafico_spese.png"]
    assert kept == dav.files["Grafici EDITH/grafico_spese.png"] and b"31/10 23:55" in kept                                 # è quella di fine mese
    CE.run(UID, at(2026, 11, 1))                                                                                          # il giorno dopo: nuovo mese, la copia resta
    assert dav.files["Grafici EDITH/Archivio mensile/2026-10 grafico_spese.png"] == kept


def test_if_the_server_was_off_on_the_last_day_the_next_run_rebuilds_the_month_with_full_data(dav, fin):
    import database as db
    db.add_transaction(UID, "spesa", "spesa", 12.0, "x", "2026-10-31 22:00:00")
    rep = CE.run(UID, at(2026, 11, 2))                                                                                    # nessuna copia di ottobre: la fa adesso
    assert "2026-10 grafico_spese.png" in rep["archived"]
    assert fin["pie"][-1][1] == "Spese ottobre 2026 (finale)" and fin["rows"][-1] == 60.0                                  # 18 + 30 + i 12 del 31 (con la copia notturna sarebbero mancati)


def test_a_month_with_no_expenses_leaves_the_files_alone(dav, fin):
    CE.run(UID, at(2026, 10, 6))
    before = dict(dav.files)
    rep = CE.run(UID, at(2026, 11, 1))
    assert rep["updated"] == [] and any("nessuna spesa" in n for n in rep["notes"])
    assert {k: v for k, v in dav.files.items() if "Archivio" not in k} == {k: v for k, v in before.items() if "Archivio" not in k}   # restano quelli di ottobre
    assert "Archivio mensile/2026-10 grafico_spese.png" in "".join(dav.files)                                              # e ottobre è stato archiviato


def test_without_budgets_only_the_expenses_chart(dav, fin):
    import database as db
    with db.get_conn() as conn:
        conn.execute("DELETE FROM budgets WHERE user_id=?", (UID,))
    rep = CE.run(UID, at(2026, 10, 6))
    assert rep["updated"] == ["grafico_spese.png"] and "Grafici EDITH/budget.png" not in dav.files


def test_the_year_boundary_archives_december(dav, fin):
    import database as db
    db.add_transaction(UID, "spesa", "spesa", 10.0, "x", "2025-12-10 12:00:00")
    db.add_transaction(UID, "spesa", "spesa", 10.0, "x", "2026-01-05 12:00:00")
    rep = CE.run(UID, at(2026, 1, 15))
    assert rep["archived"] == ["2025-12 grafico_spese.png", "2025-12 budget.png"] and "Grafici EDITH/Archivio mensile/2025-12 grafico_spese.png" in dav.files


def test_the_folder_name_is_configurable_nested_and_safe_in_urls(dav, fin, monkeypatch):
    monkeypatch.setattr(CE, "CHARTS_FOLDER", "Documenti/Grafici è più/")
    rep = CE.run(UID, at(2026, 10, 6))
    assert "Documenti/Grafici è più/grafico_spese.png" in dav.files and "Documenti/Grafici è più/Archivio mensile/2026-09 budget.png" in dav.files
    assert {"Documenti", "Documenti/Grafici è più"} <= dav.folders and rep["folder"] == "Documenti/Grafici è più"
    assert all(" " not in u and "è" not in u for u in dav.urls) and any("Grafici%20%C3%A8%20pi%C3%B9" in u for u in dav.urls)      # caratteri speciali codificati


def test_an_empty_folder_setting_falls_back_to_the_default(dav, fin, monkeypatch):
    monkeypatch.setattr(CE, "CHARTS_FOLDER", " / ")
    assert CE.run(UID, at(2026, 10, 6))["folder"] == "Grafici EDITH"


def test_describe_reports_in_two_lines():
    rep = {"folder": "Grafici EDITH", "updated": ["a.png", "b.png"], "archived": ["2026-09 a.png"], "notes": ["x"]}
    out = CE.describe(rep)
    assert out.splitlines() == ["✅ Aggiornati in «Grafici EDITH»: a.png, b.png", "🗂️ Copia mensile salvata: 2026-09 a.png", "ℹ️ x"]
    assert CE.describe({"folder": "F", "updated": [], "archived": [], "notes": []}) == "Niente da aggiornare."


# ---------------------------------------------------------------------------
# Quando qualcosa non va
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,needle", [(401, "credenziali"), (403, "permesso"), (404, "non trova la cartella"), (507, "spazio"), (423, "bloccato"), (500, "HTTP 500")])
def test_upload_errors_are_explained(dav, fin, status, needle):
    dav.fail["PUT"] = status
    with pytest.raises(CE.ChartsError) as e:
        CE.run(UID, at(2026, 10, 6))
    assert needle in str(e.value) and "grafico_spese.png" in str(e.value)


def test_a_nextcloud_that_answers_404_for_a_missing_folder_is_handled_like_409(fin, monkeypatch):
    """Il caso reale: PUT in una cartella che non esiste ancora risponde 404 invece di 409. La cartella va creata e il file salvato."""
    d = FakeDAV(missing_parent=404)
    monkeypatch.setattr(ns, "_req", d.req)
    rep = CE.run(UID, at(2026, 10, 6))
    assert rep["updated"] == ["grafico_spese.png", "budget.png"] and "Grafici EDITH/grafico_spese.png" in d.files
    assert "Grafici EDITH/Archivio mensile/2026-09 budget.png" in d.files and {"Grafici EDITH", "Grafici EDITH/Archivio mensile"} <= d.folders


def test_the_error_names_the_folder(dav, fin):
    dav.fail["PUT"] = 403
    with pytest.raises(CE.ChartsError) as e:
        CE.run(UID, at(2026, 10, 6))
    assert "«Grafici EDITH»" in str(e.value)


def test_folder_creation_errors_are_explained(dav, fin):
    dav.fail["MKCOL"] = 403
    with pytest.raises(CE.ChartsError) as e:
        CE.run(UID, at(2026, 10, 6))
    assert "Grafici EDITH" in str(e.value) and "permesso" in str(e.value)


def test_unreachable_nextcloud_is_a_clear_error(fin, monkeypatch):
    def down(method, url, **kw):
        raise ns.NotesError("Nextcloud non raggiungibile (ConnectionError)")
    monkeypatch.setattr(ns, "_req", down)
    with pytest.raises(CE.ChartsError) as e:
        CE.run(UID, at(2026, 10, 6))
    assert "non raggiungibile" in str(e.value)


@pytest.mark.parametrize("bad,needle", [(None, "nulla"), ("testo", "PNG"), (b"GIF89a....", "PNG"), (b"", "PNG"), (42, "int")])
def test_a_chart_function_returning_something_else_is_reported(dav, fin, monkeypatch, bad, needle):
    sys.modules["charts"].pie_chart_categorie = lambda rows, title="": bad
    with pytest.raises(CE.ChartsError) as e:
        CE.run(UID, at(2026, 10, 6))
    assert needle in str(e.value) and dav.files == {}                                                                    # niente di mezzo scritto


def test_a_missing_chart_module_is_a_clear_error(dav, monkeypatch):
    monkeypatch.setitem(sys.modules, "charts", None)                       # import fallisce come se non esistesse
    with pytest.raises(CE.ChartsError) as e:
        CE.run(UID, at(2026, 10, 6))
    assert "modulo dei grafici" in str(e.value)


def test_png_helper_accepts_bytes_and_buffers():
    assert CE._png(PNG + b"x", "t") == PNG + b"x" and CE._png(io.BytesIO(PNG + b"y"), "t") == PNG + b"y" and CE._png(bytearray(PNG + b"z"), "t") == PNG + b"z"


def test_info_lists_the_chart_functions():
    out = CE._info()
    assert "== charts" in out and "pie_chart_categorie" in out or "NON TROVATO" in out
