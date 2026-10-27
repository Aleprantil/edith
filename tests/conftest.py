"""Impostazioni comuni dei test: ambiente finto e database delle finanze temporaneo (codice vero del modulo finance/)."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="edith-test-"))

os.environ.update(
    TELEGRAM_BOT_TOKEN="000:test", GEMINI_API_KEY="test", TELEGRAM_CHAT_ID="42", OWNER_NAME="Tester",
    CALDAV_URL="http://127.0.0.1:9/remote.php/dav", CALDAV_USER="u", CALDAV_PASSWORD="p", TZ="Europe/Rome",
    DB_PATH=str(TMP / "finance.db"), MON_STATE_FILE=str(TMP / "mon.json"), MON_PROBE_FILE=str(TMP / "probe.json"),
    WEB_ENABLED="1", WEB_ALLOWED_LOGINS="me@example.com",
)
for p in (str(ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _memory_without_network(monkeypatch):
    """Nei test la memoria non va a cercare Nextcloud (che qui non c'è): parte vuota e ogni test può darle la sua."""
    from services import memory_service, notes_service
    monkeypatch.setattr(notes_service, "read_note_exact", lambda title, category="": None)
    memory_service.invalidate()
    memory_service._cache["facts"] = None
    yield
    memory_service.invalidate()
    memory_service._cache["facts"] = None
