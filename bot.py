import os
import re
import shutil
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Dict, List, Tuple

import aiosqlite
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    Defaults,
    filters,
)

# =========================
# CONFIG
# =========================

LEARNING_LANGS: List[Tuple[str, str]] = [
    ("🇰🇷 KOREAN", "ko"),
    ("🇯🇵 JAPANESE", "ja"),
    ("🇺🇸 ENGLISH", "en"),
]
DEFAULT_UI_LANG = "my"          # UI/guide default Myanmar
DEFAULT_LEARNING_LANG = "en"    # if user never chooses, fallback
QUIZ_DIFFICULTIES = ("Basic", "Medium", "Hard")


@dataclass(frozen=True)
class Cfg:
    db_path: str
    admin_ids: set[int]


# =========================
# DB LAYER (SQLite / aiosqlite)
# =========================

def now_iso() -> str:
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

                learning_language TEXT DEFAULT 'en',
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
                lang TEXT NOT NULL,         -- ko|ja|en
                type TEXT NOT NULL,         -- AV|VOCA|GRAM
                topic TEXT NOT NULL,        -- e.g. Numbers, Colors
                level_tag TEXT,             -- optional: Basic/Unit1/Level2
                html TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lang TEXT NOT NULL,             -- ko|ja|en
                difficulty TEXT NOT NULL,       -- Basic|Medium|Hard
                level_tag TEXT,                 -- optional label
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option TEXT NOT NULL,   -- A|B|C|D
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


async def upsert_user(cfg: Cfg, update: Update) -> None:
    u = update.effective_user
    if not u:
        return

    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,))
        row = await cur.fetchone()
        await cur.close()

        if row is None:
            await db.execute(
                """
                INSERT INTO users(user_id, username, first_name, tg_lang, learning_language, created_at, last_active)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    u.id,
                    u.username,
                    u.first_name,
                    u.language_code,
                    DEFAULT_LEARNING_LANG,
                    now_iso(),
                    now_iso(),
                ),
            )
        else:
            await db.execute(
                """
                UPDATE users
                SET username=?, first_name=?, tg_lang=?, last_active=?
                WHERE user_id=?
                """,
                (u.username, u.first_name, u.language_code, now_iso(), u.id),
            )
        await db.commit()


async def log_action(cfg: Cfg, user_id: int, action: str, meta: Optional[str] = None) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "INSERT INTO learning_logs(user_id, action, meta, ts) VALUES(?, ?, ?, ?)",
            (user_id, action, meta, now_iso()),
        )
        await db.commit()


async def get_user(cfg: Cfg, user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None


async def set_learning_language(cfg: Cfg, user_id: int, lang: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE users SET learning_language=?, last_active=? WHERE user_id=?",
            (lang, now_iso(), user_id),
        )
        await db.commit()


async def set_quiz_difficulty(cfg: Cfg, user_id: int, difficulty: str) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE users SET quiz_difficulty=?, last_active=? WHERE user_id=?",
            (difficulty, now_iso(), user_id),
        )
        await db.commit()


async def add_exp(cfg: Cfg, user_id: int, amount: int) -> Tuple[int, int, bool]:
    """
    Returns: (exp, level, leveled_up)
    Level rule: every 100 EXP => +1 level (level starts at 1)
    """
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE users SET exp=exp+?, last_active=? WHERE user_id=?",
            (amount, now_iso(), user_id),
        )
        await db.commit()

        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT exp, level FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()

        exp = int(row["exp"])
        old_level = int(row["level"])
        new_level = (exp // 100) + 1
        leveled_up = new_level != old_level

        if leveled_up:
            await db.execute("UPDATE users SET level=? WHERE user_id=?", (new_level, user_id))
            await db.commit()

        return exp, new_level, leveled_up


async def set_exp_absolute(cfg: Cfg, user_id: int, exp: int) -> None:
    exp = max(0, exp)
    level = (exp // 100) + 1
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            "UPDATE users SET exp=?, level=?, last_active=? WHERE user_id=?",
            (exp, level, now_iso(), user_id),
        )
        await db.commit()


async def list_contents(cfg: Cfg, lang: str, ctype: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, topic, level_tag
            FROM contents
            WHERE lang=? AND type=?
            ORDER BY id DESC
            """,
            (lang, ctype),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def get_content(cfg: Cfg, content_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM contents WHERE id=?", (content_id,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None


async def upsert_content(cfg: Cfg, lang: str, ctype: str, topic: str, html: str, level_tag: Optional[str]) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute(
            "SELECT id FROM contents WHERE lang=? AND type=? AND topic=?",
            (lang, ctype, topic),
        )
        row = await cur.fetchone()
        await cur.close()

        if row is None:
            await db.execute(
                """
                INSERT INTO contents(lang, type, topic, level_tag, html, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (lang, ctype, topic, level_tag, html, now_iso()),
            )
        else:
            await db.execute(
                "UPDATE contents SET level_tag=?, html=? WHERE id=?",
                (level_tag, html, row[0]),
            )
        await db.commit()


async def delete_contents_by_type(cfg: Cfg, ctype: str) -> int:
    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute("DELETE FROM contents WHERE type=?", (ctype,))
        await db.commit()
        return cur.rowcount


async def delete_quiz_all(cfg: Cfg) -> int:
    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute("DELETE FROM quiz_questions")
        await db.commit()
        return cur.rowcount


async def add_quiz_question(
    cfg: Cfg,
    lang: str,
    difficulty: str,
    level_tag: Optional[str],
    question: str,
    a: str,
    b: str,
    c: str,
    d: str,
    correct: str,
    explanation: Optional[str],
    reward: int,
) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO quiz_questions(
                lang, difficulty, level_tag, question,
                option_a, option_b, option_c, option_d,
                correct_option, explanation, exp_reward, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (lang, difficulty, level_tag, question, a, b, c, d, correct, explanation, reward, now_iso()),
        )
        await db.commit()


async def get_random_quiz(cfg: Cfg, lang: str, difficulty: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(cfg.db_path) as db:
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


async def get_quiz_by_id(cfg: Cfg, qid: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(cfg.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM quiz_questions WHERE id=?", (qid,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None


async def start_quiz_session(cfg: Cfg, user_id: int, qid: int) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute(
            """
            INSERT INTO quiz_sessions(user_id, question_id, started_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                question_id=excluded.question_id,
                started_at=excluded.started_at
            """,
            (user_id, qid, now_iso()),
        )
        await db.commit()


async def get_quiz_session(cfg: Cfg, user_id: int) -> Optional[int]:
    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute("SELECT question_id FROM quiz_sessions WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else None


async def clear_quiz_session(cfg: Cfg, user_id: int) -> None:
    async with aiosqlite.connect(cfg.db_path) as db:
        await db.execute("DELETE FROM quiz_sessions WHERE user_id=?", (user_id,))
        await db.commit()


async def top_users(cfg: Cfg, limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(cfg.db_path) as db:
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


async def all_user_ids(cfg: Cfg) -> List[int]:
    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        await cur.close()
        return [int(r[0]) for r in rows]


async def stats(cfg: Cfg) -> Dict[str, int]:
    async with aiosqlite.connect(cfg.db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = int((await cur.fetchone())[0])
        await cur.close()

        db.row_factory = aiosqlite.Row
        cur2 = await db.execute("SELECT last_active FROM users")
        rows = await cur2.fetchall()
        await cur2.close()

    now = datetime.now(timezone.utc)
    a24 = 0
    a7 = 0
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["last_active"])
        except Exception:
            continue
        if now - dt <= timedelta(hours=24):
            a24 += 1
        if now - dt <= timedelta(days=7):
            a7 += 1

    return {"total_users": total, "active_24h": a24, "active_7d": a7}


# =========================
# UI HELPERS
# =========================

def is_admin(cfg: Cfg, user_id: int) -> bool:
    return user_id in cfg.admin_ids


def safe_name(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "User"
    if u.username:
        return f"@{u.username}"
    return (u.full_name or "User").strip()


def progress_bar(exp: int, level: int) -> str:
    prev_exp = (level - 1) * 100
    next_exp = level * 100
    in_level = exp - prev_exp
    span = max(1, next_exp - prev_exp)
    pct = max(0, min(100, int((in_level / span) * 100)))
    filled = int(pct / 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"{bar}  {in_level}/{span} ({pct}%)"


def kb_learning_langs(prefix: str = "lang") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(name, callback_data=f"{prefix}:{code}")] for name, code in LEARNING_LANGS]
    return InlineKeyboardMarkup(rows)


def kb_sections() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("A&V (ဗျည်း/သရ/ပေါင်းသံ)", callback_data="sec:AV")],
            [InlineKeyboardButton("Voca (ဝေါဟာရ)", callback_data="sec:VOCA")],
            [InlineKeyboardButton("Gram (သဒ္ဒါ)", callback_data="sec:GRAM")],
        ]
    )


def kb_difficulty(current: Optional[str]) -> InlineKeyboardMarkup:
    def btn(d: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(f"{d}{' ✅' if current == d else ''}", callback_data=f"diff:{d}")

    return InlineKeyboardMarkup([[btn("Basic"), btn("Medium"), btn("Hard")]])


def kb_content_list(items: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for it in items[:30]:
        topic = it.get("topic", "Topic")
        tag = it.get("level_tag")
        label = f"{topic}" + (f" ({tag})" if tag else "")
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"content:{it['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back:home")])
    return InlineKeyboardMarkup(rows)


def kb_quiz_options(qid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("A", callback_data=f"ans:{qid}:A"),
                InlineKeyboardButton("B", callback_data=f"ans:{qid}:B"),
            ],
            [
                InlineKeyboardButton("C", callback_data=f"ans:{qid}:C"),
                InlineKeyboardButton("D", callback_data=f"ans:{qid}:D"),
            ],
        ]
    )


def kb_admin_edit() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌐 Choose Content Language (ko/ja/en)", callback_data="admin:choose_lang")],
            [InlineKeyboardButton("➕ Add/Update A&V", callback_data="admin:add:AV")],
            [InlineKeyboardButton("➕ Add/Update Voca", callback_data="admin:add:VOCA")],
            [InlineKeyboardButton("➕ Add/Update Gram", callback_data="admin:add:GRAM")],
            [InlineKeyboardButton("➕ Add Quiz Question", callback_data="admin:add:QUIZ")],
            [InlineKeyboardButton("✅ Exit", callback_data="admin:exit")],
        ]
    )


HELP_TEXT = (
    "<b>🎓 Learner (User) Commands</b>\n"
    "• /start - ဘာသာစကားရွေး + Bot သုံးပုံလမ်းညွှန်\n"
    "• /profile - Level / EXP / Learning Progress\n"
    "• /AandV - ဗျည်း၊ သရ၊ ပေါင်းစပ်သံ\n"
    "• /voca - ဝေါဟာရ (Numbers, Colors, Fruits, ...)\n"
    "• /gram - သဒ္ဒါတည်ဆောက်ပုံ\n"
    "• /quiz - Quiz ဖြေ (ဖြေတိုင်း EXP ရ)\n"
    "• /cquiz - Quiz difficulty ပြောင်း (Basic|Medium|Hard)\n"
    "• /tops - ထိပ်တန်းလေ့လာသူများ\n"
    "• /help - ဒီစာမျက်နှာ\n\n"
    "<b>⚡️ Admin Commands</b>\n"
    "• /edit - Dashboard (Choose Language → Add Content/Quiz)\n"
    "• /giveexp &lt;id&gt; &lt;amount&gt; - EXP သတ်မှတ်\n"
    "• /broadcast - အားလုံးထံ စာ/ပုံ ပို့\n"
    "• /stats - Users/Active stats\n"
    "• /backup - DB backup ထုတ်\n"
    "• /restore - DB restore (file upload)\n"
    "• /allclear - Data အားလုံးဖျက်\n"
    "• /delete &lt;type&gt; - AV|VOCA|GRAM|QUIZ ဖျက်\n"
)


# =========================
# CONVERSATION STATES
# =========================

EDIT_MENU, EDIT_WAIT_CONTENT, EDIT_WAIT_QUIZ = 10, 11, 12
BROADCAST_WAIT = 20
RESTORE_WAIT = 30
ALLCLEAR_WAIT = 40


# =========================
# LEARNER COMMANDS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    text = (
        "<b>👋 Learning Language Bot မှ ကြိုဆိုပါတယ်</b>\n\n"
        "<b>သင်ယူမယ့် ဘာသာစကားရွေးပါ</b> (Korean/Japanese/English)\n"
        "ပြီးရင် အောက်ပါ command တွေနဲ့ စလုပ်နိုင်ပါတယ်👇\n\n"
        "• /AandV • /voca • /gram • /quiz\n"
        "• /profile • /tops • /help\n\n"
        "<i>Choose your learning language:</i>"
    )
    await update.message.reply_text(text, reply_markup=kb_learning_langs(prefix="lang"))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    u = await get_user(cfg, update.effective_user.id)
    if not u:
        await update.message.reply_text("Profile မတွေ့ပါ။ /start ကိုပြန်ခေါ်ပါ။")
        return

    exp = int(u["exp"])
    level = int(u["level"])
    lang = u.get("learning_language") or DEFAULT_LEARNING_LANG
    diff = u.get("quiz_difficulty") or "Basic"

    text = (
        f"<b>👤 Profile</b>\n"
        f"• Name: <b>{safe_name(update)}</b>\n"
        f"• Learning Language: <code>{lang}</code>\n"
        f"• Level: <b>{level}</b>\n"
        f"• EXP: <b>{exp}</b>\n"
        f"• Progress: <b>{progress_bar(exp, level)}</b>\n"
        f"• Quiz Difficulty: <b>{diff}</b>\n"
    )
    await update.message.reply_text(text)


async def send_section(update: Update, context: ContextTypes.DEFAULT_TYPE, ctype: str) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    u = await get_user(cfg, update.effective_user.id)
    lang = (u.get("learning_language") if u else None) or DEFAULT_LEARNING_LANG

    items = await list_contents(cfg, lang=lang, ctype=ctype)
    if not items:
        await update.message.reply_text(
            f"<b>{ctype}</b> content မရှိသေးပါ။\nAdmin က /edit နဲ့ <code>{lang}</code> အတွက် ထည့်ပေးရပါမယ်။"
        )
        return

    title = {"AV": "A&V", "VOCA": "Voca", "GRAM": "Gram"}.get(ctype, ctype)
    await update.message.reply_text(
        f"<b>📚 {title}</b>\n(<code>{lang}</code>) ထဲက topic ကိုရွေးပါ👇",
        reply_markup=kb_content_list(items),
    )


async def cmd_aandv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_section(update, context, "AV")


async def cmd_voca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_section(update, context, "VOCA")


async def cmd_gram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_section(update, context, "GRAM")


async def cmd_cquiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    u = await get_user(cfg, update.effective_user.id)
    current = (u.get("quiz_difficulty") if u else None) or "Basic"
    await update.message.reply_text(
        "<b>🎯 Quiz Difficulty</b>\nရွေးပါ👇",
        reply_markup=kb_difficulty(current),
    )


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    u = await get_user(cfg, update.effective_user.id)
    if not u:
        await update.message.reply_text("User data မရှိသေးပါ။ /start ကိုပြန်ခေါ်ပါ။")
        return

    lang = u.get("learning_language") or DEFAULT_LEARNING_LANG
    diff = u.get("quiz_difficulty") or "Basic"

    q = await get_random_quiz(cfg, lang=lang, difficulty=diff)
    if not q:
        await update.message.reply_text(
            f"<b>🧠 Quiz</b>\n(<code>{lang}</code> / <b>{diff}</b>) မေးခွန်းမရှိသေးပါ။\nAdmin က /edit နဲ့ Quiz ထည့်ပေးရပါမယ်။"
        )
        return

    await start_quiz_session(cfg, update.effective_user.id, int(q["id"]))
    await log_action(cfg, update.effective_user.id, "quiz_start", f"qid={q['id']}")

    text = (
        f"<b>🧠 Quiz ({diff})</b>  <code>{lang}</code>\n\n"
        f"{q['question']}\n\n"
        f"<b>A)</b> {q['option_a']}\n"
        f"<b>B)</b> {q['option_b']}\n"
        f"<b>C)</b> {q['option_c']}\n"
        f"<b>D)</b> {q['option_d']}\n"
    )
    await update.message.reply_text(text, reply_markup=kb_quiz_options(int(q["id"])))


async def cmd_tops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    top = await top_users(cfg, limit=10)
    if not top:
        await update.message.reply_text("Top list မရှိသေးပါ။")
        return

    lines = ["<b>🏆 Top Learners</b>"]
    for i, u in enumerate(top, start=1):
        name = u.get("username") or u.get("first_name") or str(u["user_id"])
        lines.append(f"{i}. <b>{name}</b> — Lv <b>{u['level']}</b> | EXP <b>{u['exp']}</b>")
    await update.message.reply_text("\n".join(lines))


# =========================
# CALLBACK HANDLER (Learner)
# =========================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    q = update.callback_query
    if not q:
        return

    await q.answer()
    data = q.data or ""

    # User selects learning language
    if data.startswith("lang:"):
        lang = data.split(":", 1)[1].strip()
        if lang not in {"ko", "ja", "en"}:
            await q.edit_message_text("Language မမှန်ပါ။ /start ကိုပြန်ခေါ်ပါ။")
            return

        await upsert_user(cfg, update)
        await set_learning_language(cfg, q.from_user.id, lang)
        await log_action(cfg, q.from_user.id, "set_learning_language", lang)

        await q.edit_message_text(
            "<b>✅ ရွေးချယ်ပြီးပါပြီ</b>\n"
            f"Learning Language: <code>{lang}</code>\n\n"
            "စတင်နိုင်တဲ့ command တွေ👇\n"
            "• /AandV • /voca • /gram • /quiz\n"
            "• /profile • /tops • /help"
        )
        return

    # Difficulty selection
    if data.startswith("diff:"):
        diff = data.split(":", 1)[1].strip()
        if diff not in QUIZ_DIFFICULTIES:
            return
        await set_quiz_difficulty(cfg, q.from_user.id, diff)
        await log_action(cfg, q.from_user.id, "set_quiz_difficulty", diff)
        await q.edit_message_text(f"<b>✅ Quiz difficulty set:</b> <b>{diff}</b>")
        return

    # View content detail
    if data.startswith("content:"):
        cid = int(data.split(":", 1)[1])
        item = await get_content(cfg, cid)
        if not item:
            await q.edit_message_text("Content မတွေ့ပါ။")
            return

        title = {"AV": "A&V", "VOCA": "Voca", "GRAM": "Gram"}.get(item["type"], item["type"])
        head = f"<b>📌 {title}: {item['topic']}</b>"
        if item.get("level_tag"):
            head += f" <i>({item['level_tag']})</i>"
        head += f"\n<code>{item['lang']}</code>\n\n"
        await q.edit_message_text(head + item["html"])
        await log_action(cfg, q.from_user.id, "view_content", f"id={cid}")
        return

    if data == "back:home":
        await q.edit_message_text(
            "<b>🏠 Home</b>\n"
            "ရွေးစရာ commands👇\n"
            "• /AandV • /voca • /gram • /quiz • /profile"
        )
        return

    # Quiz answer
    if data.startswith("ans:"):
        _, qid_s, opt = data.split(":")
        qid = int(qid_s)
        opt = opt.strip().upper()

        session_qid = await get_quiz_session(cfg, q.from_user.id)
        if session_qid != qid:
            await q.edit_message_text("Session မကိုက်ညီပါ။ /quiz ကိုပြန်ခေါ်ပါ။")
            return

        qq = await get_quiz_by_id(cfg, qid)
        if not qq:
            await q.edit_message_text("Question မတွေ့ပါ။")
            await clear_quiz_session(cfg, q.from_user.id)
            return

        correct = (qq["correct_option"] or "").strip().upper()
        explanation = qq.get("explanation")
        reward_correct = int(qq.get("exp_reward") or 10)

        # Requirement: "ဖြေဆိုပြီးတိုင်း EXP ရ" => wrong လည်း အနည်းငယ်ရ
        reward_wrong = 3

        is_right = opt == correct
        gained = reward_correct if is_right else reward_wrong

        exp, level, leveled_up = await add_exp(cfg, q.from_user.id, gained)
        await clear_quiz_session(cfg, q.from_user.id)
        await log_action(cfg, q.from_user.id, "quiz_answer", f"qid={qid},opt={opt},correct={correct}")

        msg = "<b>✅ Correct!</b>" if is_right else f"<b>❌ Wrong!</b> (Correct: <b>{correct}</b>)"
        msg += f"\n• EXP +<b>{gained}</b>\n• Level: <b>{level}</b> | EXP: <b>{exp}</b>"
        if leveled_up:
            msg += "\n\n<b>🎉 Level Up!</b> ဆက်လေ့လာကြရအောင်!"
        if explanation:
            msg += f"\n\n<b>📌 Explanation</b>\n{explanation}"

        await q.edit_message_text(msg)
        return


# =========================
# ADMIN: /edit DASHBOARD (Conversation)
# =========================

async def edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    # default edit language (content language) - keep in session
    if "edit_lang" not in context.user_data:
        context.user_data["edit_lang"] = "en"

    lang = context.user_data["edit_lang"]
    text = (
        "<b>🛠 Admin Dashboard</b>\n\n"
        f"Current Content Language: <code>{lang}</code>\n\n"
        "<b>လုပ်ပုံလုပ်နည်း</b>\n"
        "1) Choose Content Language (ko/ja/en)\n"
        "2) Add/Update A&V / Voca / Gram / Quiz\n\n"
        "<i>ရွေးပါ👇</i>"
    )
    await update.message.reply_text(text, reply_markup=kb_admin_edit())
    return EDIT_MENU


async def edit_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    q = update.callback_query
    if not q:
        return EDIT_MENU
    await q.answer()

    if not is_admin(cfg, q.from_user.id):
        await q.edit_message_text("⛔ Admin only.")
        return ConversationHandler.END

    data = q.data or ""

    if data == "admin:exit":
        await q.edit_message_text("✅ Admin dashboard closed.")
        return ConversationHandler.END

    if data == "admin:choose_lang":
        await q.edit_message_text(
            "<b>🌐 Choose Content Language</b>\n"
            "အောက်ကမှ ရွေးပါ👇",
            reply_markup=kb_learning_langs(prefix="adminlang"),
        )
        return EDIT_MENU

    if data.startswith("adminlang:"):
        lang = data.split(":", 1)[1].strip()
        if lang not in {"ko", "ja", "en"}:
            return EDIT_MENU
        context.user_data["edit_lang"] = lang
        await q.edit_message_text(
            f"<b>✅ Content Language Set:</b> <code>{lang}</code>\n\n"
            "ဆက်လုပ်ရန် /edit ကိုပြန်ခေါ်လည်းရပါတယ် (သို့) အပေါ်က dashboard message ကိုအသုံးပြုပါ။"
        )
        return ConversationHandler.END

    if data.startswith("admin:add:"):
        ctype = data.split(":", 2)[2].strip()
        lang = context.user_data.get("edit_lang", "en")

        if ctype in {"AV", "VOCA", "GRAM"}:
            context.user_data["pending_add_type"] = ctype
            await q.edit_message_text(
                "<b>➕ Add/Update Content</b>\n\n"
                f"Language: <code>{lang}</code>\n"
                f"Type: <b>{ctype}</b>\n\n"
                "Template အတိုင်း message တစ်ခုတည်းနဲ့ ပို့ပါ (HTML allowed):\n\n"
                "<code>"
                "TOPIC: Numbers\n"
                "LEVEL: Basic   (optional)\n"
                "HTML:\n"
                "<b>Numbers</b><br/>\n"
                "1 = one<br/>\n"
                "2 = two\n"
                "</code>\n\n"
                "Cancel: /cancel"
            )
            return EDIT_WAIT_CONTENT

        if ctype == "QUIZ":
            context.user_data["pending_add_type"] = "QUIZ"
            await q.edit_message_text(
                "<b>➕ Add Quiz Question</b>\n\n"
                f"Language: <code>{lang}</code>\n\n"
                "Template အတိုင်းပို့ပါ:\n\n"
                "<code>"
                "DIFFICULTY: Basic\n"
                "LEVEL: Unit1   (optional)\n"
                "QUESTION: What is ...?\n"
                "A: ...\n"
                "B: ...\n"
                "C: ...\n"
                "D: ...\n"
                "CORRECT: A\n"
                "EXPLANATION: ... (optional)\n"
                "REWARD: 10\n"
                "</code>\n\n"
                "Cancel: /cancel"
            )
            return EDIT_WAIT_QUIZ

    return EDIT_MENU


def _extract_field(text: str, key: str) -> Optional[str]:
    # Match "KEY: value" (case-insensitive), first occurrence
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _extract_block_after(text: str, marker: str) -> str:
    # Get everything after a line "MARKER:" (case-insensitive)
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().upper() == f"{marker.upper()}:":
            start = i + 1
            break
    if start is None:
        return ""
    return "\n".join(lines[start:]).strip()


async def edit_receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text or ""
    lang = context.user_data.get("edit_lang", "en")
    ctype = context.user_data.get("pending_add_type")

    if ctype not in {"AV", "VOCA", "GRAM"}:
        await update.message.reply_text("State မမှန်ပါ။ /edit ကိုပြန်စပါ။")
        return ConversationHandler.END

    topic = _extract_field(text, "TOPIC")
    level_tag = _extract_field(text, "LEVEL")
    html = _extract_block_after(text, "HTML")

    if not topic or not html:
        await update.message.reply_text("❌ TOPIC နှင့် HTML မပြည့်စုံပါ။ Template အတိုင်းပြန်ပို့ပါ။")
        return EDIT_WAIT_CONTENT

    await upsert_content(cfg, lang=lang, ctype=ctype, topic=topic, html=html, level_tag=level_tag)
    await log_action(cfg, update.effective_user.id, "admin_upsert_content", f"{lang}/{ctype}/{topic}")

    await update.message.reply_text(
        f"✅ Saved!\nLanguage: <code>{lang}</code>\nType: <b>{ctype}</b>\nTopic: <b>{topic}</b>\n\n/edit နဲ့ ဆက်လုပ်နိုင်ပါတယ်။"
    )
    return ConversationHandler.END


async def edit_receive_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text or ""
    lang = context.user_data.get("edit_lang", "en")

    diff = (_extract_field(text, "DIFFICULTY") or "Basic").title()
    level_tag = _extract_field(text, "LEVEL")
    question = _extract_field(text, "QUESTION")
    a = _extract_field(text, "A")
    b = _extract_field(text, "B")
    c = _extract_field(text, "C")
    d = _extract_field(text, "D")
    correct = (_extract_field(text, "CORRECT") or "").strip().upper()
    explanation = _extract_field(text, "EXPLANATION")
    reward_s = _extract_field(text, "REWARD") or "10"

    if diff not in QUIZ_DIFFICULTIES:
        await update.message.reply_text("❌ DIFFICULTY must be Basic | Medium | Hard")
        return EDIT_WAIT_QUIZ

    if not all([question, a, b, c, d]) or correct not in {"A", "B", "C", "D"}:
        await update.message.reply_text("❌ Field မပြည့်စုံပါ။ Template အတိုင်းပြန်ပို့ပါ။")
        return EDIT_WAIT_QUIZ

    try:
        reward = int(reward_s)
    except Exception:
        reward = 10

    await add_quiz_question(cfg, lang, diff, level_tag, question, a, b, c, d, correct, explanation, reward)
    await log_action(cfg, update.effective_user.id, "admin_add_quiz", f"{lang}/{diff}")

    await update.message.reply_text(
        f"✅ Quiz question saved!\nLanguage: <code>{lang}</code>\nDifficulty: <b>{diff}</b>\n\n/edit နဲ့ ဆက်လုပ်နိုင်ပါတယ်။"
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# =========================
# ADMIN COMMANDS
# =========================

async def cmd_giveexp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /giveexp <id> <amount>")
        return
    try:
        uid = int(context.args[0])
        amount = int(context.args[1])
    except Exception:
        await update.message.reply_text("❌ id/amount must be integer.")
        return

    u = await get_user(cfg, uid)
    if not u:
        await update.message.reply_text("User မတွေ့ပါ။")
        return

    new_exp = max(0, int(u["exp"]) + amount)
    await set_exp_absolute(cfg, uid, new_exp)
    await log_action(cfg, update.effective_user.id, "admin_giveexp", f"to={uid},amount={amount}")
    await update.message.reply_text(f"✅ Updated <code>{uid}</code> EXP => <b>{new_exp}</b>")


async def cmd_broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "<b>📣 Broadcast</b>\n"
        "အခု message တစ်ခု (စာ/ပုံ) ပို့ပါ။\n"
        "Bot က users အားလုံးထံ copy ပို့ပေးပါမယ်။\n\n"
        "Cancel: /cancel"
    )
    return BROADCAST_WAIT


async def cmd_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return ConversationHandler.END

    user_ids = await all_user_ids(cfg)
    if not user_ids:
        await update.message.reply_text("Users မရှိသေးပါ။")
        return ConversationHandler.END

    sent = 0
    failed = 0

    src_chat = update.effective_chat.id
    src_msg = update.message.message_id

    await update.message.reply_text(f"Sending to <b>{len(user_ids)}</b> users...")

    for uid in user_ids:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=src_chat, message_id=src_msg)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await log_action(cfg, update.effective_user.id, "admin_broadcast", f"sent={sent},failed={failed}")
    await update.message.reply_text(f"✅ Done.\n• Sent: <b>{sent}</b>\n• Failed: <b>{failed}</b>")
    return ConversationHandler.END


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return

    s = await stats(cfg)
    await update.message.reply_text(
        "<b>📊 Bot Stats</b>\n"
        f"• Total users: <b>{s['total_users']}</b>\n"
        f"• Active (24h): <b>{s['active_24h']}</b>\n"
        f"• Active (7d): <b>{s['active_7d']}</b>\n"
    )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not os.path.exists(cfg.db_path):
        await update.message.reply_text("DB file မတွေ့ပါ။")
        return

    os.makedirs("backups", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{ts}.sqlite3"
    dst = os.path.join("backups", backup_name)

    def _copy() -> None:
        shutil.copy2(cfg.db_path, dst)

    await asyncio.to_thread(_copy)

    with open(dst, "rb") as f:
        await update.message.reply_document(document=f, filename=backup_name, caption="✅ Backup created.")


async def cmd_restore_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "<b>♻️ Restore Database</b>\n"
        "SQLite DB file (.sqlite3/.db/.sqlite) ကို upload လုပ်ပါ။\n"
        "⚠️ Restore လုပ်တာနဲ့ လက်ရှိ DB ကို အရင် backup ထုတ်ပြီး အစားထိုးပါမယ်။\n\n"
        "Cancel: /cancel"
    )
    return RESTORE_WAIT


async def cmd_restore_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return ConversationHandler.END

    doc = update.message.document
    if not doc:
        await update.message.reply_text("DB file upload လုပ်ပါ။")
        return RESTORE_WAIT

    fn = doc.file_name or ""
    if not fn.endswith((".sqlite3", ".db", ".sqlite")):
        await update.message.reply_text("❌ File type မမှန်ပါ။ .sqlite3/.db/.sqlite ဖြစ်ရမယ်။")
        return RESTORE_WAIT

    os.makedirs("restore_tmp", exist_ok=True)
    tmp_path = os.path.join("restore_tmp", fn)

    f = await doc.get_file()
    await f.download_to_drive(custom_path=tmp_path)

    # backup current
    if os.path.exists(cfg.db_path):
        os.makedirs("backups", exist_ok=True)
        shutil.copy2(cfg.db_path, os.path.join("backups", "pre_restore_backup.sqlite3"))

    shutil.copy2(tmp_path, cfg.db_path)
    await init_db(cfg.db_path)  # ensure tables exist

    await update.message.reply_text("✅ Restore done. (Backup saved: backups/pre_restore_backup.sqlite3)")
    return ConversationHandler.END


async def cmd_allclear_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "<b>⚠️ ALL CLEAR</b>\n"
        "Data အားလုံးကို အပြီးတိုင် ဖျက်ပစ်မယ်။ ဆက်လုပ်ချင်ရင် အောက်က စာသားကို ပြန်ပို့ပါ:\n\n"
        "<code>YES_DELETE_ALL</code>\n\n"
        "Cancel: /cancel"
    )
    return ALLCLEAR_WAIT


async def cmd_allclear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return ConversationHandler.END

    if (update.message.text or "").strip() != "YES_DELETE_ALL":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    if os.path.exists(cfg.db_path):
        os.makedirs("backups", exist_ok=True)
        shutil.copy2(cfg.db_path, os.path.join("backups", "pre_allclear_backup.sqlite3"))
        try:
            os.remove(cfg.db_path)
        except Exception:
            pass

    await init_db(cfg.db_path)
    await update.message.reply_text("✅ All data cleared. (Backup saved: backups/pre_allclear_backup.sqlite3)")
    return ConversationHandler.END


def normalize_delete_type(token: str) -> Optional[str]:
    t = token.strip().upper()
    if t in {"A&V", "AV", "AANDV"}:
        return "AV"
    if t in {"VOCA", "VOCAB", "VOCABULARY"}:
        return "VOCA"
    if t in {"GRAM", "GRAMMAR"}:
        return "GRAM"
    if t in {"QUIZ"}:
        return "QUIZ"
    return None


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await upsert_user(cfg, update)

    if not is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /delete <type>\nTypes: AV | VOCA | GRAM | QUIZ")
        return

    ctype = normalize_delete_type(context.args[0])
    if not ctype:
        await update.message.reply_text("❌ Unknown type. Use: AV|VOCA|GRAM|QUIZ")
        return

    if ctype == "QUIZ":
        n = await delete_quiz_all(cfg)
        await log_action(cfg, update.effective_user.id, "admin_delete_quiz_all", f"count={n}")
        await update.message.reply_text(f"✅ Deleted quiz questions: <b>{n}</b>")
        return

    n = await delete_contents_by_type(cfg, ctype)
    await log_action(cfg, update.effective_user.id, "admin_delete_content_type", f"type={ctype},count={n}")
    await update.message.reply_text(f"✅ Deleted contents ({ctype}): <b>{n}</b>")


# =========================
# MAIN
# =========================

async def post_init(app: Application) -> None:
    cfg: Cfg = app.bot_data["cfg"]
    await init_db(cfg.db_path)


def load_cfg() -> Tuple[Cfg, str]:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing in .env")

    db_path = os.getenv("DATABASE_PATH", "learning_language.sqlite3").strip()

    admin_raw = os.getenv("ADMIN_IDS", "").strip()
    admins: set[int] = set()
    for part in admin_raw.split(","):
        part = part.strip()
        if part:
            try:
                admins.add(int(part))
            except Exception:
                pass

    return Cfg(db_path=db_path, admin_ids=admins), token


def main() -> None:
    cfg, token = load_cfg()

    defaults = Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    app = (
        ApplicationBuilder()
        .token(token)
        .defaults(defaults)
        .post_init(post_init)
        .build()
    )
    app.bot_data["cfg"] = cfg

    # Learner commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("AandV", cmd_aandv))
    app.add_handler(CommandHandler("voca", cmd_voca))
    app.add_handler(CommandHandler("gram", cmd_gram))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("cquiz", cmd_cquiz))
    app.add_handler(CommandHandler("tops", cmd_tops))

    # Callback (learner actions + quiz answers)
    app.add_handler(CallbackQueryHandler(on_callback))

    # Admin conversations: /edit
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_entry)],
        states={
            EDIT_MENU: [CallbackQueryHandler(edit_menu_callback, pattern=r"^(admin:|adminlang:).+")],
            EDIT_WAIT_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_content)],
            EDIT_WAIT_QUIZ: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_quiz)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="edit_conv",
        persistent=False,
    )
    app.add_handler(edit_conv)

    # Admin: broadcast
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", cmd_broadcast_entry)],
        states={BROADCAST_WAIT: [MessageHandler(filters.ALL & ~filters.COMMAND, cmd_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="broadcast_conv",
        persistent=False,
    )
    app.add_handler(broadcast_conv)

    # Admin: restore
    restore_conv = ConversationHandler(
        entry_points=[CommandHandler("restore", cmd_restore_entry)],
        states={RESTORE_WAIT: [MessageHandler(filters.Document.ALL & ~filters.COMMAND, cmd_restore_receive)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="restore_conv",
        persistent=False,
    )
    app.add_handler(restore_conv)

    # Admin: allclear
    allclear_conv = ConversationHandler(
        entry_points=[CommandHandler("allclear", cmd_allclear_entry)],
        states={ALLCLEAR_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_allclear_confirm)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="allclear_conv",
        persistent=False,
    )
    app.add_handler(allclear_conv)

    # Admin normal commands
    app.add_handler(CommandHandler("giveexp", cmd_giveexp))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("delete", cmd_delete))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
