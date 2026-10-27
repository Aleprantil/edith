"""Avvio e spegnimento della web app dentro il ciclo asyncio del bot (con uvicorn vero, su una porta libera)."""
import asyncio
import json
import socket
import sys
import urllib.request

import config
from web import cards, server


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read().decode()


def test_server_starts_serves_and_stops_inside_a_running_loop(monkeypatch):
    port = free_port()
    monkeypatch.setattr(config, "WEB_PORT", port)
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")

    async def scenario():
        task = await server.start_web()
        assert task is not None
        for _ in range(50):                                          # attendo che accenda
            try:
                if (await asyncio.to_thread(fetch, f"http://127.0.0.1:{port}/healthz"))[0] == 200:
                    break
            except OSError:
                await asyncio.sleep(0.1)
        status, body = await asyncio.to_thread(fetch, f"http://127.0.0.1:{port}/api/me", {"Tailscale-User-Login": "me@example.com"})
        me = json.loads(body)
        ticks = 0
        for _ in range(5):                                           # il ciclo del bot resta libero mentre il server lavora
            await asyncio.sleep(0.01)
            ticks += 1
        await server.stop_web(task)
        assert task.done()
        try:
            await asyncio.to_thread(fetch, f"http://127.0.0.1:{port}/healthz")
            stopped = False
        except OSError:
            stopped = True
        return status, me, ticks, stopped

    status, me, ticks, stopped = asyncio.run(scenario())
    assert status == 200 and me["user"] == "me@example.com" and ticks == 5 and stopped
    assert cards.OWNER_ID == 42                                       # TELEGRAM_CHAT_ID dei test


def test_port_already_in_use_does_not_crash_or_hang_the_bot(monkeypatch):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        monkeypatch.setattr(config, "WEB_PORT", busy.getsockname()[1])

        async def scenario():
            task = await server.start_web()
            await asyncio.wait_for(asyncio.shield(task), timeout=5)    # deve terminare da solo, con un errore nei log
            await server.stop_web(task)
            return task.done()
        assert asyncio.run(scenario())


def test_missing_uvicorn_is_not_fatal(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    assert asyncio.run(server.start_web()) is None
    asyncio.run(server.stop_web(None))


def test_owner_id_falls_back_to_configured_users(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "non numerico")
    monkeypatch.setattr(config, "FINANCE_USER_IDS", {99, 7})
    assert server.owner_id() == 7
    monkeypatch.setattr(config, "FINANCE_USER_IDS", set())
    monkeypatch.setattr(config, "CALENDAR_USER_IDS", set())
    assert server.owner_id() == 0


def test_settings_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "WEB_ALLOWED_LOGINS", ["a@b.it"])
    monkeypatch.setattr(config, "WEB_PASSWORD", "pw")
    st = server.build_settings()
    assert st.allowed_logins == ["a@b.it"] and st.password == "pw" and st.secret and st.configured


def test_web_only_mode_runs_without_the_telegram_bot():
    """`python3 -m web.only PORTA` deve servire la pagina da solo (processo vero, con l'ambiente dei test)."""
    import os
    import subprocess
    import time
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    port = free_port()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests" / "fixtures" / "finance")])}
    proc = subprocess.Popen([sys.executable, "-m", "web.only", str(port)], cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        ok = False
        for _ in range(60):
            try:
                if fetch(f"http://127.0.0.1:{port}/healthz")[0] == 200:
                    ok = True
                    break
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
        assert ok, proc.stdout.read() if proc.poll() is not None else "non è partito"
        status, body = fetch(f"http://127.0.0.1:{port}/api/me", {"Tailscale-User-Login": "me@example.com"})
        assert status == 200 and json.loads(body)["user"] == "me@example.com"
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    assert "SOLO WEB" in out
