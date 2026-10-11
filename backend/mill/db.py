"""SQLite access: connections, creating/upgrading the schema at startup, a write lock, and small row helpers."""
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date

from fastapi import HTTPException

from mill.config import BACKEND, DB_PATH


def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def migrate(db):
    """Columns added after a database was first created; CREATE TABLE IF NOT EXISTS can't add them."""
    for table, col, ddl in [("material_issues", "rate", "REAL"), ("ai_reviews", "auto", "INTEGER NOT NULL DEFAULT 0"),
                            ("ai_reviews", "final", "TEXT"), ("machines", "rate_m_per_hour", "REAL NOT NULL DEFAULT 0"),
                            ("job_cards", "operator_id", "INTEGER REFERENCES workers"),
                            ("job_cards", "shift_id", "INTEGER REFERENCES shifts"), ("job_cards", "running_since", "TEXT"),
                            ("job_cards", "last_tick", "TEXT"),
                            ("job_cards", "released", "INTEGER NOT NULL DEFAULT 0"), ("production_entries", "auto", "INTEGER NOT NULL DEFAULT 0"),
                            ("buyer_messages", "external_id", "TEXT"), ("bot_conversations", "difficulty", "TEXT"),
                            ("bot_conversations", "model", "TEXT"),
                            ("users", "email", "TEXT"), ("users", "phone", "TEXT")]:
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init():
    """Create or upgrade the schema. Run once at startup; safe to repeat."""
    db = connect()
    db.execute("PRAGMA journal_mode = WAL")
    migrate(db)
    for f in ("schema.sql", "automation.sql"):
        db.executescript(open(os.path.join(BACKEND, "db", f)).read())
    db.execute("INSERT OR IGNORE INTO app_secrets VALUES ('jwt', ?)", (secrets.token_urlsafe(48),))  # see auth.jwt_secret
    # Issues made before rates were stored get the all-time average they used to be costed at.
    db.execute("""UPDATE material_issues SET rate = (SELECT SUM(g.qty_kg * g.rate) / SUM(g.qty_kg) FROM grn g
                  WHERE g.yarn_type_id = material_issues.yarn_type_id) WHERE rate IS NULL""")
    db.close()


def get_db():
    db = connect()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def tx(db):
    """Write lock for check-then-write sequences (stock, dispatch guards)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def rows(cur):
    return [dict(r) for r in cur]


def one(db, sql, *args):
    r = db.execute(sql, args).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    return dict(r)


# Columns whose schema default is SQLite's CURRENT_DATE, which is UTC: in India that is yesterday until 05:30,
# right through the night shift. Every insert goes through here, so they get the local date instead.
LOCAL_DATE = {"grn": "received_on", "material_issues": "issued_on", "rolls": "made_on", "invoices": "issued_on",
              "payments": "paid_on"}


def insert(db, table, data):
    """table and keys come from code or are checked against the schema, never raw user input."""
    if (col := LOCAL_DATE.get(table)) and not data.get(col):
        data = {**data, col: date.today().isoformat()}
    return db.execute(f"INSERT INTO {table} ({', '.join(data)}) VALUES ({', '.join(':' + k for k in data)})",
                      data).lastrowid


def month_of(month: str | None):
    return month or date.today().strftime("%Y-%m")
