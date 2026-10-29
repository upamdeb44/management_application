"""
Database layer.

* Locally (no DATABASE_URL set)  -> a SQLite file (planner.db), exactly like before.
* Deployed (DATABASE_URL set)    -> PostgreSQL (e.g. a free Neon database). The URL can come from the
  DATABASE_URL environment variable or from Streamlit secrets.
The rest of the code doesn't care which one is used.
"""
import os
import sqlite3
import hashlib
import secrets
import time as time_module
from datetime import datetime, time, timedelta

DATABASE_NAME = os.environ.get("SQLITE_PATH", "planner.db")
SESSION_DAYS = 30


def _find_database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:                                    # Streamlit Community Cloud keeps it in "Secrets"
        import streamlit as st
        return st.secrets.get("DATABASE_URL") or None
    except Exception:
        return None


DATABASE_URL = _find_database_url()
IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import psycopg2
    import psycopg2.pool
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
    PK = "SERIAL PRIMARY KEY"
else:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
    PK = "INTEGER PRIMARY KEY AUTOINCREMENT"


# ---------- PostgreSQL: pooled connections behind the same API the SQLite code uses ----------
_pool = None
_last_used = {}


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 10, dsn=DATABASE_URL, connect_timeout=10,
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
        )
    return _pool


class _PgCursor:
    """Lets the existing SQL (written with ? placeholders) run on PostgreSQL."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        self._cursor.execute(sql.replace("?", "%s"), params if params else None)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description


class _PgConnection:
    def __init__(self, conn):
        self._conn = conn
        self._closed = False

    def cursor(self):
        return _PgCursor(self._conn.cursor())

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.rollback()          # discards anything uncommitted (e.g. after an error)
        except Exception:
            pass
        _last_used[id(self._conn)] = time_module.time()
        try:
            _get_pool().putconn(self._conn)
        except Exception:
            pass


def _checkout_postgres():
    pool = _get_pool()
    for _ in range(6):
        try:
            conn = pool.getconn()
        except psycopg2.pool.PoolError:    # all connections busy: wait a moment
            time_module.sleep(0.2)
            continue
        try:
            if conn.closed:
                raise psycopg2.OperationalError("connection closed")
            if time_module.time() - _last_used.get(id(conn), 0) > 30:   # idle a while: make sure it's alive
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.rollback()
            return _PgConnection(conn)
        except Exception:
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
    raise RuntimeError("Could not connect to the database. Check DATABASE_URL.")


def get_connection():
    if IS_POSTGRES:
        return _checkout_postgres()
    connection = sqlite3.connect(DATABASE_NAME)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


_schema_ready = False


def create_table():
    global _schema_ready
    if _schema_ready:      # only needs to run once per server process
        return
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {PK},
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS tasks (
            id {PK},
            user_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            due_date TEXT NOT NULL,
            due_time TEXT NOT NULL,
            category TEXT,
            priority TEXT,
            reminder_3_days INTEGER DEFAULT 0,
            reminder_2_days INTEGER DEFAULT 0,
            reminder_1_day INTEGER DEFAULT 0,
            reminder_1_hour INTEGER DEFAULT 0,
            recurrence_type TEXT DEFAULT 'Does not repeat',
            recurrence_interval INTEGER DEFAULT 1,
            recurrence_days TEXT DEFAULT '',
            recurrence_end_date TEXT,
            completed INTEGER DEFAULT 0,
            reminder_at_time INTEGER DEFAULT 1,
            reminder_custom_minutes INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Migration for databases created by older versions of the app
    columns = [
        ("reminder_3_days", "INTEGER DEFAULT 0"),
        ("reminder_2_days", "INTEGER DEFAULT 0"),
        ("reminder_1_day", "INTEGER DEFAULT 0"),
        ("reminder_1_hour", "INTEGER DEFAULT 0"),
        ("recurrence_type", "TEXT DEFAULT 'Does not repeat'"),
        ("recurrence_interval", "INTEGER DEFAULT 1"),
        ("recurrence_days", "TEXT DEFAULT ''"),
        ("recurrence_end_date", "TEXT"),
        ("completed", "INTEGER DEFAULT 0"),
        ("user_id", "INTEGER"),
        ("reminder_at_time", "INTEGER DEFAULT 1"),      # existing tasks also notify at their due time
        ("reminder_custom_minutes", "INTEGER DEFAULT 0"),
    ]
    if not IS_POSTGRES:    # a fresh PostgreSQL database already has every column
        for column_name, column_type in columns:
            try:
                cursor.execute(f"ALTER TABLE tasks ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError:
                pass

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS task_occurrence_status (
            id {PK},
            task_id INTEGER NOT NULL,
            occurrence_date TEXT NOT NULL,
            completed INTEGER DEFAULT 0,
            UNIQUE(task_id, occurrence_date),
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)

    # User-defined task categories (the built-in ones live in the app)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS categories (
            id {PK},
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    # category names are unique per user, ignoring upper/lower case
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_user_name ON categories (user_id, lower(name))"
    )

    # Login sessions (the browser keeps the random token in a cookie; only its hash is stored)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Reminders that have already fired (prevents duplicates) + the notification inbox
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS reminder_log (
            id {PK},
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            occurrence_date TEXT NOT NULL,
            reminder_label TEXT NOT NULL,
            title TEXT,
            message TEXT,
            created_at TEXT NOT NULL,
            dismissed INTEGER DEFAULT 0,
            UNIQUE(task_id, occurrence_date, reminder_label),
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    connection.commit()
    connection.close()
    _schema_ready = True


# ---------------------------------------------------------
# PASSWORDS / USERS
# ---------------------------------------------------------
def hash_password(password):
    salt = secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000
    ).hex()
    return f"{salt}${password_hash}"


def verify_password(password, stored_hash):
    try:
        salt, password_hash = stored_hash.split("$")
        new_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000
        ).hex()
        return secrets.compare_digest(new_hash, password_hash)
    except (ValueError, AttributeError):
        return False


def create_user(name, email, password):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        insert_sql = """
            INSERT INTO users (name, email, password_hash, created_at)
            VALUES (?, ?, ?, ?)
        """
        params = (name.strip(), email.strip().lower(), hash_password(password),
                  datetime.now().isoformat())
        if IS_POSTGRES:
            cursor.execute(insert_sql + " RETURNING id", params)
            user_id = cursor.fetchone()[0]
        else:
            cursor.execute(insert_sql, params)
            user_id = cursor.lastrowid

        # Legacy tasks without an owner are claimed ONLY by the very first user.
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 1:
            cursor.execute("UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (user_id,))

        connection.commit()
        return True, user_id, ""
    except INTEGRITY_ERRORS:
        return False, None, "An account with this email already exists."
    finally:
        connection.close()


def authenticate_user(email, password):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT id, name, email, password_hash FROM users WHERE email = ?",
        (email.strip().lower(),),
    )
    user = cursor.fetchone()
    connection.close()

    if not user:
        return None
    user_id, name, user_email, stored_hash = user
    if verify_password(password, stored_hash):
        return {"id": user_id, "name": name, "email": user_email}
    return None


# ---------------------------------------------------------
# CUSTOM CATEGORIES
# ---------------------------------------------------------
def _clean_category(name):
    return " ".join(str(name or "").split())[:30]


def get_categories(user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT name FROM categories WHERE user_id = ? ORDER BY lower(name)", (user_id,)
    )
    names = [row[0] for row in cursor.fetchall()]
    connection.close()
    return names


def add_category(user_id, name):
    """Returns True if added, False if empty or it already exists."""
    name = _clean_category(name)
    if not name:
        return False
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO categories (user_id, name) VALUES (?, ?) ON CONFLICT DO NOTHING", (user_id, name)
    )
    added = cursor.rowcount == 1
    connection.commit()
    connection.close()
    return added


def delete_category(user_id, name):
    connection = get_connection()
    connection.execute(
        "DELETE FROM categories WHERE user_id = ? AND lower(name) = lower(?)", (user_id, name)
    )
    connection.commit()
    connection.close()


# ---------------------------------------------------------
# LOGIN SESSIONS (stay logged in across refreshes)
# ---------------------------------------------------------
def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat()
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now().isoformat(),))
    cursor.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), user_id, expires),
    )
    connection.commit()
    connection.close()
    return token


def get_user_by_session(token):
    if not token or not isinstance(token, str):
        return None
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT u.id, u.name, u.email
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_at > ?
    """, (_hash_token(token), datetime.now().isoformat()))
    row = cursor.fetchone()
    connection.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2]}


def delete_session(token):
    if not token:
        return
    connection = get_connection()
    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    connection.commit()
    connection.close()


# ---------------------------------------------------------
# REMINDER NOTIFICATIONS
# ---------------------------------------------------------
def claim_reminder(user_id, task_id, occurrence_date, label, title, message, silent=False):
    """Record that a reminder has fired. Returns True only the first time (so it never repeats)."""
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        INSERT INTO reminder_log
            (user_id, task_id, occurrence_date, reminder_label, title, message, created_at, dismissed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
    """, (user_id, task_id, occurrence_date, label, title, message,
          datetime.now().isoformat(), 1 if silent else 0))
    claimed = cursor.rowcount == 1
    connection.commit()
    connection.close()
    return claimed


def get_active_notifications(user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT id, title, message, created_at FROM reminder_log
        WHERE user_id = ? AND dismissed = 0 ORDER BY id DESC
    """, (user_id,))
    rows = [{"id": r[0], "title": r[1], "message": r[2], "created_at": r[3]} for r in cursor.fetchall()]
    connection.close()
    return rows


def dismiss_notification(notification_id, user_id):
    connection = get_connection()
    connection.execute(
        "UPDATE reminder_log SET dismissed = 1 WHERE id = ? AND user_id = ?",
        (notification_id, user_id),
    )
    connection.commit()
    connection.close()


def dismiss_all_notifications(user_id):
    connection = get_connection()
    connection.execute("UPDATE reminder_log SET dismissed = 1 WHERE user_id = ?", (user_id,))
    connection.commit()
    connection.close()


# ---------------------------------------------------------
# TASKS
# ---------------------------------------------------------
def _time_sort_key(due_time):
    """Turn '06:00 PM' (or 24h formats) into a time so sorting is chronological."""
    for fmt in ("%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(due_time), fmt).time()
        except ValueError:
            pass
    return time(18, 0)


def add_work(user_id, title, description, due_date, due_time, category, priority,
             reminder_3_days, reminder_2_days, reminder_1_day, reminder_1_hour,
             recurrence_type="Does not repeat", recurrence_interval=1,
             recurrence_days="", recurrence_end_date=None,
             reminder_at_time=False, reminder_custom_minutes=0):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        INSERT INTO tasks (
            user_id, title, description, due_date, due_time, category, priority,
            reminder_3_days, reminder_2_days, reminder_1_day, reminder_1_hour,
            recurrence_type, recurrence_interval, recurrence_days, recurrence_end_date,
            reminder_at_time, reminder_custom_minutes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, title, description, due_date, due_time, category, priority,
          int(reminder_3_days), int(reminder_2_days), int(reminder_1_day), int(reminder_1_hour),
          recurrence_type, int(recurrence_interval), recurrence_days, recurrence_end_date,
          int(reminder_at_time), int(reminder_custom_minutes or 0)))
    connection.commit()
    connection.close()


def get_all_works(user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT id, title, description, due_date, due_time, category, priority,
               reminder_3_days, reminder_2_days, reminder_1_day, reminder_1_hour, completed,
               recurrence_type, recurrence_interval, recurrence_days, recurrence_end_date,
               COALESCE(reminder_at_time, 0), COALESCE(reminder_custom_minutes, 0)
        FROM tasks WHERE user_id = ?
    """, (user_id,))
    works = cursor.fetchall()
    connection.close()
    # due_time is text like "06:00 PM", so sort chronologically in Python
    works.sort(key=lambda w: (w[3], _time_sort_key(w[4])))
    return works


def mark_completed(task_id, user_id, completed):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE tasks SET completed = ? WHERE id = ? AND user_id = ?",
        (int(completed), task_id, user_id),
    )
    connection.commit()
    connection.close()


def mark_occurrence_completed(task_id, user_id, occurrence_date, completed):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT id FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    if not cursor.fetchone():
        connection.close()
        return
    cursor.execute("""
        INSERT INTO task_occurrence_status (task_id, occurrence_date, completed)
        VALUES (?, ?, ?)
        ON CONFLICT(task_id, occurrence_date) DO UPDATE SET completed = excluded.completed
    """, (task_id, occurrence_date, int(completed)))
    connection.commit()
    connection.close()


def get_completed_dates(task_id, user_id):
    """All completed occurrence dates (ISO strings) for one task, in a single query."""
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT occurrence_date FROM task_occurrence_status
        WHERE task_id = ? AND completed = 1
          AND task_id IN (SELECT id FROM tasks WHERE user_id = ?)
    """, (task_id, user_id))
    dates = {row[0] for row in cursor.fetchall()}
    connection.close()
    return dates


def delete_work(task_id, user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "DELETE FROM task_occurrence_status WHERE task_id = ? "
        "AND task_id IN (SELECT id FROM tasks WHERE user_id = ?)",
        (task_id, user_id),
    )
    cursor.execute(
        "DELETE FROM reminder_log WHERE task_id = ? AND user_id = ?", (task_id, user_id)
    )
    cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    connection.commit()
    connection.close()


def get_work_by_task_id(task_id, user_id):
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT id, title, description, due_date, due_time, category, priority,
               reminder_3_days, reminder_2_days, reminder_1_day, reminder_1_hour,
               recurrence_type, recurrence_interval, recurrence_days, recurrence_end_date,
               COALESCE(reminder_at_time, 0) AS reminder_at_time,
               COALESCE(reminder_custom_minutes, 0) AS reminder_custom_minutes
        FROM tasks WHERE id = ? AND user_id = ?
    """, (task_id, user_id))
    row = cursor.fetchone()
    if row:
        columns = [col[0] for col in cursor.description]
        work = dict(zip(columns, row))
    else:
        work = None
    connection.close()
    return work


def update_work(task_id, user_id, title, description, due_date, due_time, category, priority,
                reminder_3_days, reminder_2_days, reminder_1_day, reminder_1_hour,
                recurrence_type, recurrence_interval, recurrence_days, recurrence_end_date,
                reminder_at_time=False, reminder_custom_minutes=0):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "SELECT due_date, recurrence_type, recurrence_interval FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    )
    old = cursor.fetchone()
    if not old:
        connection.close()
        return

    cursor.execute("""
        UPDATE tasks SET
            title=?, description=?, due_date=?, due_time=?, category=?, priority=?,
            reminder_3_days=?, reminder_2_days=?, reminder_1_day=?, reminder_1_hour=?,
            recurrence_type=?, recurrence_interval=?, recurrence_days=?, recurrence_end_date=?,
            reminder_at_time=?, reminder_custom_minutes=?
        WHERE id=? AND user_id=?
    """, (title, description, due_date, due_time, category, priority,
          int(reminder_3_days), int(reminder_2_days), int(reminder_1_day), int(reminder_1_hour),
          recurrence_type, int(recurrence_interval or 1), recurrence_days,
          recurrence_end_date, int(reminder_at_time), int(reminder_custom_minutes or 0),
          task_id, user_id))

    # Completion marks are keyed by date. If the schedule changed they no longer
    # line up with any occurrence, so clear them instead of leaving orphans.
    old_date, old_type, old_interval = old
    if (old_date != due_date or old_type != recurrence_type
            or int(old_interval or 1) != int(recurrence_interval or 1)):
        cursor.execute("DELETE FROM task_occurrence_status WHERE task_id = ?", (task_id,))

    # The due time / reminders may have changed, so let this task's reminders fire again
    cursor.execute("DELETE FROM reminder_log WHERE task_id = ?", (task_id,))

    connection.commit()
    connection.close()