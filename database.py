import aiosqlite
import os
from contextlib import asynccontextmanager

DB_PATH = os.getenv("DB_PATH", "database.db")

@asynccontextmanager
async def get_db():
    """হাই-কনকারেন্সিতে ডেটাবেজ লক রোধ করার জন্য অপ্টিমাইজড কানেকশন হেল্পার"""
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute("PRAGMA busy_timeout = 5000")
        db.row_factory = aiosqlite.Row
        yield db

async def init_db():
    async with get_db() as db:
        # পারফরম্যান্স বুস্ট ও নন-ব্লকিং রিড/রাইট
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                api_key TEXT,
                is_banned INTEGER DEFAULT 0
            )
        """)
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
                phone TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_activations_user ON activations(user_id)")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')")
        await db.commit()

async def add_user(user_id: int):
    async with get_db() as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
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

async def update_api_key(user_id: int, api_key: str):
    async with get_db() as db:
        await db.execute("UPDATE users SET api_key = ? WHERE user_id = ?", (api_key, user_id))
        await db.commit()

async def set_ban_status(user_id: int, is_banned: bool):
    async with get_db() as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if is_banned else 0, user_id))
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

async def save_activation(activation_id: str, user_id: int, phone: str):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO activations (activation_id, user_id, phone) VALUES (?, ?, ?)",
            (str(activation_id), user_id, phone)
        )
        await db.commit()

async def get_activation_user(activation_id: str):
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, phone FROM activations WHERE activation_id = ?",
            (str(activation_id),)
        ) as cur:
            row = await cur.fetchone()
            return (row["user_id"], row["phone"]) if row else None

async def delete_activation(activation_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM activations WHERE activation_id = ?", (str(activation_id),))
        await db.commit()
