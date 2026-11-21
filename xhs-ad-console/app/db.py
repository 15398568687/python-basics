import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("APP_DB", "data/console.sqlite3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, daily_quota INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
 tenant_id TEXT NOT NULL REFERENCES tenants(id),
 role TEXT NOT NULL CHECK(role IN ('manager','operator','client'))
);
CREATE TABLE IF NOT EXISTS access (
 user_id INTEGER REFERENCES users(id), account_id TEXT REFERENCES accounts(id),
 PRIMARY KEY(user_id, account_id)
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
 id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 actor INTEGER NOT NULL REFERENCES users(id), source TEXT NOT NULL,
 digest TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lines (
 import_id INTEGER NOT NULL REFERENCES imports(id), day TEXT NOT NULL, plan TEXT NOT NULL,
 spend_cents INTEGER NOT NULL, impressions INTEGER NOT NULL, clicks INTEGER NOT NULL,
 leads INTEGER, interactions INTEGER, orders INTEGER, revenue_cents INTEGER
);
CREATE INDEX IF NOT EXISTS lines_by_import ON lines(import_id,day,plan);
CREATE TABLE IF NOT EXISTS active_days (
 account_id TEXT REFERENCES accounts(id), day TEXT, import_id INTEGER NOT NULL REFERENCES imports(id),
 PRIMARY KEY(account_id,day)
);
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 title TEXT NOT NULL, source TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_by_tenant ON documents(tenant_id);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
 actor INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','review','published','failed')),
 step INTEGER NOT NULL DEFAULT 0, work TEXT NOT NULL DEFAULT '{}',
 accepted TEXT, feedback TEXT, error TEXT, lease TEXT, lease_until INTEGER,
 created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(account_id,actor,day)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), account_id TEXT,
 actor INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, detail TEXT NOT NULL,
 created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS audit_by_tenant ON audit(tenant_id,id);
CREATE TABLE IF NOT EXISTS usage (
 id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), account_id TEXT NOT NULL,
 feature TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL,
 output_tokens INTEGER NOT NULL, cost REAL NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

@contextmanager
def connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


def password_hash(password, salt=None):
    salt = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"{salt.hex()}:{digest.hex()}"


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def audit(db, user, action, detail, account=None):
    db.execute(
        "INSERT INTO audit(tenant_id,account_id,actor,action,detail) VALUES(?,?,?,?,?)",
        (user["tenant_id"], account, user["id"], action, detail),
    )


class Problem(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def account_scope(db, user, account_id, write=False):
    if write and user["role"] == "client":
        raise Problem(403, "只读账号无编辑权限")
    account = db.execute(
        """SELECT a.* FROM accounts a WHERE a.id=? AND a.tenant_id=? AND
        (?='manager' OR EXISTS(SELECT 1 FROM access WHERE user_id=? AND account_id=a.id))""",
        (account_id, user["tenant_id"], user["role"], user["id"]),
    ).fetchone()
    if account is None:
        raise Problem(403, "无权访问该账户")
    return account
