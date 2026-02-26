import aiosqlite
from typing import Optional, Any
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                tg_lang TEXT,
                chosen_language TEXT DEFAULT 'en',
                exp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                quiz_difficulty TEXT DEFAULT 'Basic',
                created_at TEXT NOT NULL,
                last_active TEXT NOT NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                meta TEXT,
                ts TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS contents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lang TEXT NOT NULL,
                type TEXT NOT NULL,            -- AV | VOCA | GRAM
                key TEXT NOT NULL,             -- topic/category/structure name
                level_tag TEXT,                -- optional label (e.g., Basic/Unit1)
                html TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lang TEXT NOT NULL,
                difficulty TEXT NOT NULL,      -- Basic | Medium | Hard
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option TEXT NOT NULL,  -- A|B|C|D
                explanation TEXT,
                exp_reward INTEGER DEFAULT 10,
                created_at TEXT NOT NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_sessions (
                user_id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(question_id) REFERENCES quiz_questions(id) ON DELETE CASCADE
            );
            """
        )

        await db.commit()


async def upsert_user(
    db_path: str,
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    tg_lang: Optional[str],
) -> None:
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()

        if row is None:
            await db.execute(
                """
                INSERT INTO users(user_id, username, first_name, tg_lang, created_at, last_active)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, first_name, tg_lang, now, now),
            )
        else:
            await db.execute(
                """
                UPDATE users
                SET username=?, first_name=?, tg_lang=?, last_active=?
                WHERE user_id=?
                """,
                (username, first_name, tg_lang, now, user_id),
            )
        await db.commit()


async def set_user_language(db_path: str, user_id: int, chosen_language: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET chosen_language=?, last_active=? WHERE user_id=?",
            (chosen_language, _now_iso(), user_id),
        )
        await db.commit()


async def get_user(db_path: str, user_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None


async def log_action(db_path: str, user_id: int, action: str, meta: Optional[str] = None) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO learning_logs(user_id, action, meta, ts) VALUES(?, ?, ?, ?)",
            (user_id, action, meta, _now_iso()),
        )
        await db.commit()


async def list_contents(db_path: str, lang: str, ctype: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, key, level_tag
            FROM contents
            WHERE lang=? AND type=?
            ORDER BY id DESC
            """,
            (lang, ctype),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def get_content_by_id(db_path: str, content_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM contents WHERE id=?", (content_id,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None


async def upsert_content(
    db_path: str,
    lang: str,
    ctype: str,
    key: str,
    html: str,
    level_tag: Optional[str] = None,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT id FROM contents WHERE lang=? AND type=? AND key=?",
            (lang, ctype, key),
        )
        row = await cur.fetchone()
        await cur.close()

        if row is None:
            await db.execute(
                """
                INSERT INTO contents(lang, type, key, level_tag, html, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (lang, ctype, key, level_tag, html, _now_iso()),
            )
        else:
            await db.execute(
                """
                UPDATE contents SET level_tag=?, html=? WHERE id=?
                """,
                (level_tag, html, row[0]),
            )
        await db.commit()


async def delete_content_type(db_path: str, ctype: str, lang: Optional[str] = None) -> int:
    async with aiosqlite.connect(db_path) as db:
        if lang:
            cur = await db.execute("DELETE FROM contents WHERE type=? AND lang=?", (ctype, lang))
        else:
            cur = await db.execute("DELETE FROM contents WHERE type=?", (ctype,))
        await db.commit()
        return cur.rowcount


async def set_quiz_difficulty(db_path: str, user_id: int, difficulty: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET quiz_difficulty=?, last_active=? WHERE user_id=?",
            (difficulty, _now_iso(), user_id),
        )
        await db.commit()


async def add_quiz_question(
    db_path: str,
    lang: str,
    difficulty: str,
    question: str,
    a: str,
    b: str,
    c: str,
    d: str,
    correct: str,
    explanation: Optional[str],
    exp_reward: int,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO quiz_questions(
                lang, difficulty, question,
                option_a, option_b, option_c, option_d,
                correct_option, explanation, exp_reward, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (lang, difficulty, question, a, b, c, d, correct, explanation, exp_reward, _now_iso()),
        )
        await db.commit()


async def delete_quiz(db_path: str, lang: Optional[str] = None) -> int:
    async with aiosqlite.connect(db_path) as db:
        if lang:
            cur = await db.execute("DELETE FROM quiz_questions WHERE lang=?", (lang,))
        else:
            cur = await db.execute("DELETE FROM quiz_questions")
        await db.commit()
        return cur.rowcount


async def get_random_quiz_question(db_path: str, lang: str, difficulty: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT *
            FROM quiz_questions
            WHERE lang=? AND difficulty=?
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (lang, difficulty),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None


async def start_quiz_session(db_path: str, user_id: int, question_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO quiz_sessions(user_id, question_id, started_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET question_id=excluded.question_id, started_at=excluded.started_at
            """,
            (user_id, question_id, _now_iso()),
        )
        await db.commit()


async def get_quiz_session(db_path: str, user_id: int) -> Optional[int]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT question_id FROM quiz_sessions WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else None


async def clear_quiz_session(db_path: str, user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM quiz_sessions WHERE user_id=?", (user_id,))
        await db.commit()


async def add_exp(db_path: str, user_id: int, amount: int) -> dict[str, Any]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("UPDATE users SET exp = exp + ?, last_active=? WHERE user_id=?", (amount, _now_iso(), user_id))
        await db.commit()

        cur = await db.execute("SELECT exp, level FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        exp = int(row["exp"])
        level = int(row["level"])

        new_level = (exp // 100) + 1
        if new_level != level:
            await db.execute("UPDATE users SET level=? WHERE user_id=?", (new_level, user_id))
            await db.commit()
            level = new_level

        return {"exp": exp, "level": level, "leveled_up": new_level != int(row["level"])}


async def set_exp(db_path: str, user_id: int, exp: int) -> None:
    level = (exp // 100) + 1
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET exp=?, level=?, last_active=? WHERE user_id=?",
            (exp, level, _now_iso(), user_id),
        )
        await db.commit()


async def get_top_users(db_path: str, limit: int = 10) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT user_id, username, first_name, exp, level
            FROM users
            ORDER BY exp DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def get_all_user_ids(db_path: str) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        await cur.close()
        return [int(r[0]) for r in rows]


async def get_stats(db_path: str) -> dict[str, int]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = int((await cur.fetchone())[0])
        await cur.close()

        # last_active is ISO; for rough stats we can compare strings by datetime parsing in Python if needed,
        # but keep it simple: pull and count in Python.
        db.row_factory = aiosqlite.Row
        cur2 = await db.execute("SELECT last_active FROM users")
        rows = await cur2.fetchall()
        await cur2.close()

    from datetime import timedelta

    now = datetime.now(timezone.utc)
    active_24h = 0
    active_7d = 0
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["last_active"])
        except Exception:
            continue
        if now - dt <= timedelta(hours=24):
            active_24h += 1
        if now - dt <= timedelta(days=7):
            active_7d += 1

    return {"total_users": total, "active_24h": active_24h, "active_7d": active_7d}
