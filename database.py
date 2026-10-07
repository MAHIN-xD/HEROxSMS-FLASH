import aiosqlite
import os
from contextlib import asynccontextmanager

DB_PATH = os.getenv("DB_PATH", "database.db")

@asynccontextmanager
async def get_db():
    """High-concurrency database connection helper with WAL & busy timeout"""
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout = 5000")
        db.row_factory = aiosqlite.Row
        yield db

async def init_db():
    async with get_db() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                api_key TEXT,
                is_banned INTEGER DEFAULT 0,
                is_approved INTEGER DEFAULT 0,
                expiry_date TEXT,
                total_purchased INTEGER DEFAULT 0,
                total_otps INTEGER DEFAULT 0
            )
        """)
        
        # Schema migration check: jodi ager database file thake kintu notun column na thake
        try:
            await db.execute("ALTER TABLE users ADD COLUMN username TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN expiry_date TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN total_purchased INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN total_otps INTEGER DEFAULT 0")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activations (
                activation_id TEXT PRIMARY KEY,
                user_id INTEGER,
                phone TEXT,
                message_id INTEGER
            )
        """)
        try:
            await db.execute("ALTER TABLE activations ADD COLUMN message_id INTEGER")
        except Exception:
            pass

        await db.execute("CREATE INDEX IF NOT EXISTS idx_activations_user ON activations(user_id)")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('restock_monitor', '0')")
        await db.commit()

async def add_user(user_id: int, username: str = None, full_name: str = None):
    async with get_db() as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name) 
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                username = COALESCE(?, users.username),
                full_name = COALESCE(?, users.full_name)
        """, (user_id, username, full_name, username, full_name))
        await db.commit()

async def get_user(user_id: int):
    async with get_db() as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

async def get_all_users():
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

async def get_approved_users():
    async with get_db() as db:
        async with db.execute("SELECT * FROM users WHERE is_approved = 1 AND is_banned = 0") as cur:
            return await cur.fetchall()

async def update_api_key(user_id: int, api_key: str):
    async with get_db() as db:
        await db.execute("UPDATE users SET api_key = ? WHERE user_id = ?", (api_key, user_id))
        await db.commit()

async def set_ban_status(user_id: int, is_banned: bool):
    async with get_db() as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if is_banned else 0, user_id))
        await db.commit()

async def set_approval_status(user_id: int, is_approved: bool):
    async with get_db() as db:
        await db.execute("UPDATE users SET is_approved = ? WHERE user_id = ?", (1 if is_approved else 0, user_id))
        await db.commit()

async def set_user_subscription(user_id: int, days: int = None):
    async with get_db() as db:
        if days is None:
            exp_str = "LIFETIME"
        else:
            from datetime import datetime, timezone, timedelta
            exp = datetime.now(timezone.utc) + timedelta(days=days)
            exp_str = exp.strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("UPDATE users SET is_approved = 1, expiry_date = ? WHERE user_id = ?", (exp_str, user_id))
        await db.commit()

async def extend_user_subscription(user_id: int, days: int):
    async with get_db() as db:
        async with db.execute("SELECT expiry_date FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            current_exp = row["expiry_date"]

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        if not current_exp or current_exp == "LIFETIME":
            base_date = now
        else:
            try:
                base_date = datetime.strptime(current_exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if base_date < now:
                    base_date = now
            except Exception:
                base_date = now

        new_exp = base_date + timedelta(days=days)
        new_exp_str = new_exp.strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("UPDATE users SET is_approved = 1, expiry_date = ? WHERE user_id = ?", (new_exp_str, user_id))
        await db.commit()
        return new_exp_str

async def increment_user_stats(user_id: int, purchased: int = 0, otps: int = 0):
    async with get_db() as db:
        await db.execute("""
            UPDATE users 
            SET total_purchased = COALESCE(total_purchased, 0) + ?,
                total_otps = COALESCE(total_otps, 0) + ?
            WHERE user_id = ?
        """, (purchased, otps, user_id))
        await db.commit()

async def get_setting(key: str):
    async with get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_setting(key: str, value: str):
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def save_activation(activation_id: str, user_id: int, phone: str, message_id: int = None):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO activations (activation_id, user_id, phone, message_id) VALUES (?, ?, ?, ?)",
            (str(activation_id), user_id, phone, message_id)
        )
        await db.commit()

async def get_activation(activation_id: str):
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, phone, message_id FROM activations WHERE activation_id = ?",
            (str(activation_id),)
        ) as cur:
            return await cur.fetchone()

async def get_activation_user(activation_id: str):
    row = await get_activation(activation_id)
    if row:
        return (row["user_id"], row["phone"])
    return None

async def delete_activation(activation_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM activations WHERE activation_id = ?", (str(activation_id),))
        await db.commit()
