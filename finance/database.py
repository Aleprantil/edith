"""
Database SQLite per FinanceBot
"""

import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "financebot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            type        TEXT NOT NULL CHECK(type IN ('spesa','entrata')),
            category    TEXT NOT NULL,
            amount      REAL NOT NULL,
            note        TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            category    TEXT NOT NULL,
            monthly_limit REAL NOT NULL, -- <--- CAMBIATO DA weekly_limit
            UNIQUE(user_id, category)
        );

        CREATE TABLE IF NOT EXISTS goals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            target      REAL NOT NULL,
            saved       REAL NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id     INTEGER PRIMARY KEY,
            currency    TEXT DEFAULT '€',
            notify_daily  INTEGER DEFAULT 1,
            notify_weekly INTEGER DEFAULT 1,
            notify_budget INTEGER DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, created_at);
        """)
    with get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(budgets)")]
        if "weekly_limit" in cols and "monthly_limit" not in cols:
            conn.execute("ALTER TABLE budgets RENAME COLUMN weekly_limit TO monthly_limit")
            print("🔧 Migrata colonna budgets.weekly_limit -> monthly_limit")
    print("✅ Database inizializzato")


# ─── Transazioni ────────────────────────────────────────────────

def add_transaction(user_id, tx_type, category, amount, note="", created_at=None):
    with get_conn() as conn:
        if created_at:
            conn.execute(
                "INSERT INTO transactions (user_id, type, category, amount, note, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, tx_type, category, amount, note, created_at)
            )
        else:
            conn.execute(
                "INSERT INTO transactions (user_id, type, category, amount, note) VALUES (?,?,?,?,?)",
                (user_id, tx_type, category, amount, note)
            )


def get_transactions(user_id, from_date=None, to_date=None, tx_type=None):
    query = "SELECT * FROM transactions WHERE user_id=?"
    params = [user_id]
    if from_date:
        query += " AND date(created_at) >= date(?)"
        params.append(from_date)
    if to_date:
        query += " AND date(created_at) <= date(?)"
        params.append(to_date)
    if tx_type:
        query += " AND type=?"
        params.append(tx_type)
    query += " ORDER BY created_at DESC"
    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


def get_last_transaction(user_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()


def delete_transaction(tx_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))


def get_category_totals(user_id, from_date, to_date, tx_type="spesa"):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT category, SUM(amount) as total
            FROM transactions
            WHERE user_id=? AND type=? AND date(created_at) BETWEEN date(?) AND date(?)
            GROUP BY category
            ORDER BY total DESC
        """, (user_id, tx_type, from_date, to_date)).fetchall()
    return rows


def get_daily_totals(user_id, from_date, to_date, tx_type="spesa"):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT date(created_at) as day, SUM(amount) as total
            FROM transactions
            WHERE user_id=? AND type=? AND date(created_at) BETWEEN date(?) AND date(?)
            GROUP BY day ORDER BY day
        """, (user_id, tx_type, from_date, to_date)).fetchall()
    return rows


def search_transactions(user_id, keyword):
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM transactions
            WHERE user_id=? AND (category LIKE ? OR note LIKE ?)
            ORDER BY created_at DESC LIMIT 20
        """, (user_id, f"%{keyword}%", f"%{keyword}%")).fetchall()


# ─── Budget ─────────────────────────────────────────────────────

def set_budget(user_id, category, monthly_limit):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO budgets (user_id, category, monthly_limit)
            VALUES (?,?,?)
            ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit=excluded.monthly_limit
        """, (user_id, category, monthly_limit))


def get_budgets(user_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM budgets WHERE user_id=?", (user_id,)
        ).fetchall()


def get_budget(user_id, category):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM budgets WHERE user_id=? AND category=?",
            (user_id, category)
        ).fetchone()


# ─── Obiettivi di risparmio ──────────────────────────────────────

def add_goal(user_id, name, target):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO goals (user_id, name, target) VALUES (?,?,?)",
            (user_id, name, target)
        )


def get_goals(user_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM goals WHERE user_id=? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()


def update_goal(goal_id, amount):
    with get_conn() as conn:
        conn.execute(
            "UPDATE goals SET saved = MIN(saved + ?, target) WHERE id=?",
            (amount, goal_id)
        )


# ─── Impostazioni utente ────────────────────────────────────────

def get_all_users():
    with get_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM transactions"
        ).fetchall()]


def get_settings(user_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,)
            )
            return {"currency": "€", "notify_daily": 1, "notify_weekly": 1, "notify_budget": 1}
        return dict(row)
