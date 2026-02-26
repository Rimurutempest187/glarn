import os
import shutil
import asyncio
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
    Defaults,
)

import db as dbm
import keyboards as kb
from utils import is_admin, safe_name, parse_type_token, clamp_int


# --------- Conversation States ----------
BC_WAIT_MESSAGE = 10
RESTORE_WAIT_FILE = 20
ALLCLEAR_CONFIRM = 30


@dataclass(frozen=True)
class Cfg:
    db_path: str
    admin_ids: set[int]


HELP_TEXT = (
    "<b>🎓 Learner Commands</b>\n"
    "• /start - ဘာသာစကားရွေး + လမ်းညွှန်\n"
    "• /profile - Level/EXP/Progress\n"
    "• /AandV - ဗျည်း/သရ/ပေါင်းသံ\n"
    "• /voca - ဝေါဟာရ (category)\n"
    "• /gram - သဒ္ဒါ\n"
    "• /quiz - Quiz ဖြေ (EXP ရ)\n"
    "• /cquiz - Quiz difficulty ပြောင်း\n"
    "• /tops - Top learners\n"
    "• /help - ဒီစာမျက်နှာ\n\n"
    "<b>⚡️ Admin Commands</b>\n"
    "• /edit - Content/Quiz ထည့်/ပြင်\n"
    "• /giveexp &lt;id&gt; &lt;amount&gt;\n"
    "• /broadcast - အားလုံးကို စာ/ပုံ ပို့\n"
    "• /stats - Users/Active stats\n"
    "• /backup - DB backup ထုတ်\n"
    "• /restore - DB restore (file upload)\n"
    "• /allclear - Data အားလုံးဖျက်\n"
    "• /delete &lt;type&gt; [lang]\n"
)


def compute_progress(exp: int, level: int) -> str:
    next_level_exp = level * 100
    prev_level_exp = (level - 1) * 100
    in_level = exp - prev_level_exp
    span = next_level_exp - prev_level_exp
    pct = int((in_level / span) * 100) if span > 0 else 0
    pct = clamp_int(pct, 0, 100)
    return f"{in_level}/{span} ({pct}%)"


async def ensure_user(cfg: Cfg, update: Update) -> None:
    if not update.effective_user:
        return
    u = update.effective_user
    await dbm.upsert_user(
        cfg.db_path,
        user_id=u.id,
        username=u.username,
        first_name=u.first_name,
        tg_lang=u.language_code,
    )


# ---------------- Learner Commands ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    text = (
        "<b>👋 Welcome to Learning Language Bot</b>\n\n"
        "1) အောက်ကနေ သင်ယူမယ့် ဘာသာစကားကို ရွေးပါ\n"
        "2) ပြီးရင် /AandV /voca /gram /quiz နဲ့ စတင်နိုင်ပါတယ်\n\n"
        "<i>Choose your learning language:</i>"
    )
    await update.message.reply_text(text, reply_markup=kb.lang_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    u = await dbm.get_user(cfg.db_path, update.effective_user.id)
    if not u:
        await update.message.reply_text("Profile မတွေ့ပါ။ /start ကိုပြန်ခေါ်ပါ။")
        return

    exp = int(u["exp"])
    level = int(u["level"])
    chosen = u.get("chosen_language", "en")
    diff = u.get("quiz_difficulty", "Basic")

    progress = compute_progress(exp, level)

    text = (
        f"<b>👤 Profile</b>\n"
        f"• Name: {safe_name(update.effective_user)}\n"
        f"• Language: <code>{chosen}</code>\n"
        f"• Level: <b>{level}</b>\n"
        f"• EXP: <b>{exp}</b>\n"
        f"• Progress to next level: <b>{progress}</b>\n"
        f"• Quiz Difficulty: <b>{diff}</b>\n"
    )
    await update.message.reply_text(text)


async def _send_section_list(update: Update, context: ContextTypes.DEFAULT_TYPE, ctype: str) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    user = await dbm.get_user(cfg.db_path, update.effective_user.id)
    if not user:
        await update.message.reply_text("User data မရှိသေးပါ။ /start ကိုပြန်ခေါ်ပါ။")
        return

    lang = user.get("chosen_language", "en")
    items = await dbm.list_contents(cfg.db_path, lang=lang, ctype=ctype)

    if not items:
        await update.message.reply_text(
            f"<b>{ctype}</b> အတွက် content မထည့်ထားသေးပါ။ Admin ကို /edit နဲ့ထည့်ခိုင်းပါ။"
        )
        return

    title = {"AV": "A&V", "VOCA": "Voca", "GRAM": "Gram"}.get(ctype, ctype)
    await update.message.reply_text(
        f"<b>📚 {title}</b>\nရွေးပြီးလေ့လာပါ 👇",
        reply_markup=kb.content_list_keyboard(items),
    )


async def cmd_aandv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_section_list(update, context, "AV")


async def cmd_voca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_section_list(update, context, "VOCA")


async def cmd_gram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_section_list(update, context, "GRAM")


async def cmd_cquiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    user = await dbm.get_user(cfg.db_path, update.effective_user.id)
    cur = user.get("quiz_difficulty", "Basic") if user else "Basic"
    await update.message.reply_text(
        "<b>🎯 Quiz Difficulty</b>\nရွေးပါ 👇",
        reply_markup=kb.difficulty_keyboard(cur),
    )


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    user = await dbm.get_user(cfg.db_path, update.effective_user.id)
    if not user:
        await update.message.reply_text("User data မရှိသေးပါ။ /start ကိုပြန်ခေါ်ပါ။")
        return

    lang = user.get("chosen_language", "en")
    difficulty = user.get("quiz_difficulty", "Basic")

    q = await dbm.get_random_quiz_question(cfg.db_path, lang=lang, difficulty=difficulty)
    if not q:
        await update.message.reply_text(
            f"<b>Quiz</b> မေးခွန်းမရှိသေးပါ (<code>{lang}</code> / <b>{difficulty}</b>)။ Admin ကို /edit နဲ့ထည့်ခိုင်းပါ။"
        )
        return

    await dbm.start_quiz_session(cfg.db_path, update.effective_user.id, int(q["id"]))

    text = (
        f"<b>🧠 Quiz ({difficulty})</b>\n\n"
        f"{q['question']}\n\n"
        f"<b>A)</b> {q['option_a']}\n"
        f"<b>B)</b> {q['option_b']}\n"
        f"<b>C)</b> {q['option_c']}\n"
        f"<b>D)</b> {q['option_d']}\n"
    )
    await update.message.reply_text(text, reply_markup=kb.quiz_keyboard(int(q["id"])))
    await dbm.log_action(cfg.db_path, update.effective_user.id, "quiz_start", f"qid={q['id']}")


async def cmd_tops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    top = await dbm.get_top_users(cfg.db_path, limit=10)
    if not top:
        await update.message.reply_text("Top list မရှိသေးပါ။")
        return

    lines = ["<b>🏆 Top Learners</b>"]
    for i, u in enumerate(top, start=1):
        name = u.get("username") or u.get("first_name") or str(u["user_id"])
        lines.append(f"{i}. <b>{name}</b> — Lv {u['level']} | EXP {u['exp']}")
    await update.message.reply_text("\n".join(lines))


# ---------------- Callback Queries ----------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    q = update.callback_query
    await q.answer()

    data = q.data or ""

    # Language select
    if data.startswith("lang:"):
        code = data.split(":", 1)[1].strip()
        await ensure_user(cfg, update)
        await dbm.set_user_language(cfg.db_path, q.from_user.id, code)
        await dbm.log_action(cfg.db_path, q.from_user.id, "set_language", code)

        await q.edit_message_text(
            f"<b>✅ Language set:</b> <code>{code}</code>\n\n"
            "Commands:\n"
            "• /AandV • /voca • /gram • /quiz\n"
            "• /profile • /tops • /help"
        )
        return

    # Difficulty set
    if data.startswith("diff:"):
        diff = data.split(":", 1)[1].strip()
        await dbm.set_quiz_difficulty(cfg.db_path, q.from_user.id, diff)
        await dbm.log_action(cfg.db_path, q.from_user.id, "set_difficulty", diff)
        await q.edit_message_text(f"<b>✅ Quiz difficulty set:</b> <b>{diff}</b>")
        return

    # Content view
    if data.startswith("content:"):
        content_id = int(data.split(":", 1)[1])
        item = await dbm.get_content_by_id(cfg.db_path, content_id)
        if not item:
            await q.edit_message_text("Content မတွေ့ပါ။")
            return
        title = {"AV": "A&V", "VOCA": "Voca", "GRAM": "Gram"}.get(item["type"], item["type"])
        head = f"<b>📌 {title}: {item['key']}</b>"
        if item.get("level_tag"):
            head += f" <i>({item['level_tag']})</i>"
        await q.edit_message_text(head + "\n\n" + item["html"])
        await dbm.log_action(cfg.db_path, q.from_user.id, "view_content", f"id={content_id}")
        return

    if data == "back:home":
        await q.edit_message_text("<b>Home</b>\nရွေးစရာတွေ👇\n• /AandV • /voca • /gram • /quiz • /profile", reply_markup=None)
        return

    # Quiz answer
    if data.startswith("q:"):
        parts = data.split(":")
        qid = int(parts[1])
        opt = parts[2].strip().upper()

        session_qid = await dbm.get_quiz_session(cfg.db_path, q.from_user.id)
        if session_qid != qid:
            await q.edit_message_text("Session မကိုက်ညီပါ။ /quiz ကိုပြန်ခေါ်ပါ။")
            return

        # fetch question
        # (simple fetch via random function won't work; use direct SQL here by reusing random getter logic)
        import aiosqlite
        async with aiosqlite.connect(cfg.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM quiz_questions WHERE id=?", (qid,))
            row = await cur.fetchone()
            await cur.close()

        if not row:
            await q.edit_message_text("Question မတွေ့ပါ။")
            await dbm.clear_quiz_session(cfg.db_path, q.from_user.id)
            return

        correct = row["correct_option"].upper()
        explanation = row["explanation"]
        reward_correct = int(row["exp_reward"] or 10)
        reward_wrong = 3  # requirement: "ဖြေဆိုပြီးတိုင်း EXP ရရှိ" → အမှားလည်း နည်းနည်းရ

        is_right = (opt == correct)
        gained = reward_correct if is_right else reward_wrong

        stats = await dbm.add_exp(cfg.db_path, q.from_user.id, gained)
        await dbm.clear_quiz_session(cfg.db_path, q.from_user.id)
        await dbm.log_action(cfg.db_path, q.from_user.id, "quiz_answer", f"qid={qid},opt={opt},correct={correct}")

        msg = "<b>✅ Correct!</b>" if is_right else f"<b>❌ Wrong!</b> (Correct: <b>{correct}</b>)"
        msg += f"\n• Gained EXP: <b>+{gained}</b>\n• Level: <b>{stats['level']}</b> | EXP: <b>{stats['exp']}</b>"
        if explanation:
            msg += f"\n\n<b>📌 Explanation</b>\n{explanation}"

        await q.edit_message_text(msg)
        return

    # Admin panel callbacks handled in /edit conversation (optional)
    if data.startswith("admin:"):
        await q.answer("Use /edit command.")
        return


# ---------------- Admin Commands ----------------

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return

    user = await dbm.get_user(cfg.db_path, update.effective_user.id)
    cur_lang = user.get("chosen_language", "en") if user else "en"

    text = (
        "<b>🛠 Admin Edit Dashboard</b>\n\n"
        f"Current language: <code>{cur_lang}</code>\n\n"
        "<b>How to use</b>\n"
        "1) /start မှာ language ရွေးထားတာကို Admin language အဖြစ်သုံးမယ်\n"
        "2) Content ထည့်ရန်\n"
        "   - A&V/Voca/Gram: button နှိပ်ပြီး template အတိုင်း စာပို့\n"
        "   - Quiz: Quiz template အတိုင်း စာပို့\n\n"
        "<i>Choose:</i>"
    )
    await update.message.reply_text(text, reply_markup=kb.admin_edit_keyboard())


async def admin_add_content_template(update: Update, context: ContextTypes.DEFAULT_TYPE, ctype: str) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    user = await dbm.get_user(cfg.db_path, update.effective_user.id)
    lang = user.get("chosen_language", "en") if user else "en"

    if ctype != "QUIZ":
        context.user_data["admin_add_type"] = ctype
        await update.message.reply_text(
            "<b>🧩 Add/Update Content</b>\n\n"
            f"Language: <code>{lang}</code>\n"
            f"Type: <b>{ctype}</b>\n\n"
            "အောက်က Template အတိုင်းပို့ပါ (HTML allowed):\n\n"
            "<code>"
            "KEY: Numbers\n"
            "LEVEL: Basic   (optional)\n"
            "HTML:\n"
            "<b>Numbers</b><br/>\n"
            "1 = one<br/>\n"
            "2 = two\n"
            "</code>\n\n"
            "ပို့ပြီးတာနဲ့ auto save လုပ်ပေးမယ်။"
        )
        return BC_WAIT_MESSAGE

    # QUIZ template
    context.user_data["admin_add_type"] = "QUIZ"
    await update.message.reply_text(
        "<b>🧠 Add Quiz Question</b>\n\n"
        f"Language: <code>{lang}</code>\n\n"
        "Template အတိုင်းပို့ပါ:\n\n"
        "<code>"
        "DIFFICULTY: Basic\n"
        "QUESTION: What is the color of the sky?\n"
        "A: Blue\n"
        "B: Green\n"
        "C: Red\n"
        "D: Yellow\n"
        "CORRECT: A\n"
        "EXPLANATION: Usually the sky looks blue.\n"
        "REWARD: 10\n"
        "</code>\n"
    )
    return BC_WAIT_MESSAGE


async def admin_parse_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg.admin_ids):
        return ConversationHandler.END

    msg = update.message
    text = msg.text or ""
    add_type: Optional[str] = context.user_data.get("admin_add_type")
    user = await dbm.get_user(cfg.db_path, update.effective_user.id)
    lang = user.get("chosen_language", "en") if user else "en"

    if not add_type:
        await msg.reply_text("State မမှန်ပါ။ /edit ကိုပြန်စပါ။")
        return ConversationHandler.END

    def get_field(prefix: str) -> Optional[str]:
        for line in text.splitlines():
            if line.strip().upper().startswith(prefix.upper() + ":"):
                return line.split(":", 1)[1].strip()
        return None

    if add_type in {"AV", "VOCA", "GRAM"}:
        key = get_field("KEY")
        level_tag = get_field("LEVEL")
        html_idx = None
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip().upper() == "HTML:":
                html_idx = i + 1
                break
        html = "\n".join(lines[html_idx:]).strip() if html_idx is not None else ""

        if not key or not html:
            await msg.reply_text("❌ KEY နှင့် HTML မပါ/မမှန်ပါ။ Template အတိုင်းပြန်ပို့ပါ။")
            return BC_WAIT_MESSAGE

        await dbm.upsert_content(cfg.db_path, lang=lang, ctype=add_type, key=key, html=html, level_tag=level_tag)
        await dbm.log_action(cfg.db_path, update.effective_user.id, "admin_upsert_content", f"{lang}/{add_type}/{key}")
        await msg.reply_text(f"✅ Saved: <code>{lang}</code> / <b>{add_type}</b> / <b>{key}</b>\n/edit နဲ့ ဆက်လုပ်နိုင်ပါတယ်။")
        return ConversationHandler.END

    if add_type == "QUIZ":
        difficulty = (get_field("DIFFICULTY") or "Basic").title()
        question = get_field("QUESTION")
        a = get_field("A")
        b = get_field("B")
        c = get_field("C")
        d = get_field("D")
        correct = (get_field("CORRECT") or "").strip().upper()
        explanation = get_field("EXPLANATION")
        reward_s = get_field("REWARD") or "10"

        if difficulty not in {"Basic", "Medium", "Hard"}:
            await msg.reply_text("❌ DIFFICULTY must be Basic|Medium|Hard")
            return BC_WAIT_MESSAGE

        if not all([question, a, b, c, d]) or correct not in {"A", "B", "C", "D"}:
            await msg.reply_text("❌ Fields မပြည့်စုံပါ။ Template အတိုင်းပြန်ပို့ပါ။")
            return BC_WAIT_MESSAGE

        try:
            reward = int(reward_s)
        except Exception:
            reward = 10

        await dbm.add_quiz_question(cfg.db_path, lang, difficulty, question, a, b, c, d, correct, explanation, reward)
        await dbm.log_action(cfg.db_path, update.effective_user.id, "admin_add_quiz", f"{lang}/{difficulty}")
        await msg.reply_text("✅ Quiz question saved.\n/edit နဲ့ ဆက်လုပ်နိုင်ပါတယ်။")
        return ConversationHandler.END

    await msg.reply_text("Unknown admin add type.")
    return ConversationHandler.END


async def cmd_giveexp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
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

    user = await dbm.get_user(cfg.db_path, uid)
    if not user:
        await update.message.reply_text("User မတွေ့ပါ။")
        return

    new_exp = max(0, int(user["exp"]) + amount)
    await dbm.set_exp(cfg.db_path, uid, new_exp)
    await dbm.log_action(cfg.db_path, update.effective_user.id, "admin_giveexp", f"to={uid},amount={amount}")
    await update.message.reply_text(f"✅ Updated user <code>{uid}</code> EXP to <b>{new_exp}</b>.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return

    st = await dbm.get_stats(cfg.db_path)
    await update.message.reply_text(
        "<b>📊 Stats</b>\n"
        f"• Total users: <b>{st['total_users']}</b>\n"
        f"• Active (24h): <b>{st['active_24h']}</b>\n"
        f"• Active (7d): <b>{st['active_7d']}</b>\n"
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /delete <type> [lang]\nTypes: AV | VOCA | GRAM | QUIZ")
        return

    ctype = parse_type_token(context.args[0])
    lang = context.args[1] if len(context.args) >= 2 else None

    if not ctype:
        await update.message.reply_text("❌ Unknown type. Use: AV|VOCA|GRAM|QUIZ")
        return

    if ctype == "QUIZ":
        n = await dbm.delete_quiz(cfg.db_path, lang=lang)
        await update.message.reply_text(f"✅ Deleted quiz questions: <b>{n}</b>" + (f" for <code>{lang}</code>" if lang else " (all languages)"))
        return

    n = await dbm.delete_content_type(cfg.db_path, ctype=ctype, lang=lang)
    await update.message.reply_text(f"✅ Deleted contents: <b>{n}</b>" + (f" for <code>{lang}</code>" if lang else " (all languages)"))


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return

    src = cfg.db_path
    if not os.path.exists(src):
        await update.message.reply_text("DB file မတွေ့ပါ။")
        return

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{ts}.sqlite3"
    os.makedirs("backups", exist_ok=True)
    dst = os.path.join("backups", backup_name)

    def _copy() -> None:
        shutil.copy2(src, dst)

    await asyncio.to_thread(_copy)
    await update.message.reply_document(document=open(dst, "rb"), filename=backup_name, caption="✅ Backup created.")


async def cmd_restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "<b>♻️ Restore</b>\n"
        "SQLite DB file (.sqlite3) ကို upload လုပ်ပါ။\n"
        "⚠️ Restore လုပ်တာနဲ့ အဟောင်း DB ကို အရင် backup တစ်份 ထုတ်ပြီး အစားထိုးပါမယ်။"
    )
    return RESTORE_WAIT_FILE


async def cmd_restore_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]

    doc = update.message.document
    if not doc:
        await update.message.reply_text("DB file upload လုပ်ပါ။")
        return RESTORE_WAIT_FILE

    if not (doc.file_name or "").endswith((".sqlite3", ".db", ".sqlite")):
        await update.message.reply_text("❌ File type မမှန်ပါ။ .sqlite3/.db/.sqlite ဖြစ်ရမယ်။")
        return RESTORE_WAIT_FILE

    os.makedirs("restore_tmp", exist_ok=True)
    tmp_path = os.path.join("restore_tmp", doc.file_name)

    file = await doc.get_file()
    await file.download_to_drive(custom_path=tmp_path)

    # backup current
    if os.path.exists(cfg.db_path):
        os.makedirs("backups", exist_ok=True)
        bak_path = os.path.join("backups", "pre_restore_backup.sqlite3")
        shutil.copy2(cfg.db_path, bak_path)

    shutil.copy2(tmp_path, cfg.db_path)
    await dbm.init_db(cfg.db_path)  # ensure schema exists (if older db)
    await update.message.reply_text("✅ Restore done. (Old DB backed up as backups/pre_restore_backup.sqlite3)")
    return ConversationHandler.END


async def cmd_allclear_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "<b>⚠️ ALL CLEAR</b>\n"
        "Data အားလုံးကို အပြီးတိုင်ဖျက်မယ်။ ဆက်လုပ်ချင်ရင်\n\n"
        "<code>YES_DELETE_ALL</code>\n\n"
        "လို့ ပြန်ပို့ပါ။"
    )
    return ALLCLEAR_CONFIRM


async def cmd_allclear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if (update.message.text or "").strip() != "YES_DELETE_ALL":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    # backup before delete
    if os.path.exists(cfg.db_path):
        os.makedirs("backups", exist_ok=True)
        shutil.copy2(cfg.db_path, os.path.join("backups", "pre_allclear_backup.sqlite3"))

    # remove db and recreate
    try:
        os.remove(cfg.db_path)
    except Exception:
        pass

    await dbm.init_db(cfg.db_path)
    await update.message.reply_text("✅ All data cleared. (Backup saved: backups/pre_allclear_backup.sqlite3)")
    return ConversationHandler.END


async def cmd_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    await ensure_user(cfg, update)

    if not is_admin(update.effective_user.id, cfg.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "<b>📣 Broadcast</b>\n"
        "အခု message တစ်ခု (စာ/ပုံ/စတစ်ကာ မဟုတ်ပဲ စာ/ပုံ လုံလောက်) ပို့ပါ။\n"
        "Bot က အဲဒီ message ကို users အားလုံးထံ copy ပို့ပေးမယ်။\n\n"
        "Cancel: /cancel"
    )
    return BC_WAIT_MESSAGE


async def cmd_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Cfg = context.bot_data["cfg"]
    if not is_admin(update.effective_user.id, cfg.admin_ids):
        return ConversationHandler.END

    user_ids = await dbm.get_all_user_ids(cfg.db_path)
    if not user_ids:
        await update.message.reply_text("Users မရှိသေးပါ။")
        return ConversationHandler.END

    sent = 0
    failed = 0

    src_chat_id = update.effective_chat.id
    src_msg_id = update.message.message_id

    await update.message.reply_text(f"Sending to <b>{len(user_ids)}</b> users...")

    for uid in user_ids:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_msg_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # small throttling

    await dbm.log_action(cfg.db_path, update.effective_user.id, "admin_broadcast", f"sent={sent},failed={failed}")
    await update.message.reply_text(f"✅ Broadcast done.\n• Sent: <b>{sent}</b>\n• Failed: <b>{failed}</b>")
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------- Main ----------------

def load_config() -> Cfg:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing in .env")

    admin_raw = os.getenv("ADMIN_IDS", "").strip()
    admin_ids = set()
    for part in admin_raw.split(","):
        part = part.strip()
        if part:
            try:
                admin_ids.add(int(part))
            except Exception:
                pass

    db_path = os.getenv("DATABASE_PATH", "learning_language.sqlite3").strip()
    return Cfg(db_path=db_path, admin_ids=admin_ids)


async def post_init(app: Application) -> None:
    cfg: Cfg = app.bot_data["cfg"]
    await dbm.init_db(cfg.db_path)


def main() -> None:
    cfg = load_config()

    defaults = Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    app = (
        ApplicationBuilder()
        .token(os.getenv("BOT_TOKEN"))
        .defaults(defaults)
        .post_init(post_init)
        .build()
    )

    app.bot_data["cfg"] = cfg

    # Learner
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("AandV", cmd_aandv))
    app.add_handler(CommandHandler("voca", cmd_voca))
    app.add_handler(CommandHandler("gram", cmd_gram))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("cquiz", cmd_cquiz))
    app.add_handler(CommandHandler("tops", cmd_tops))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # Admin: /edit provides dashboard; then quick-add via commands below
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("giveexp", cmd_giveexp))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("delete", cmd_delete))

    # Conversations: broadcast / restore / allclear / admin add templates
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", cmd_broadcast_start)],
        states={BC_WAIT_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, cmd_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="broadcast_conv",
        persistent=False,
    )
    app.add_handler(broadcast_conv)

    restore_conv = ConversationHandler(
        entry_points=[CommandHandler("restore", cmd_restore_start)],
        states={RESTORE_WAIT_FILE: [MessageHandler(filters.Document.ALL & ~filters.COMMAND, cmd_restore_receive_file)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="restore_conv",
        persistent=False,
    )
    app.add_handler(restore_conv)

    allclear_conv = ConversationHandler(
        entry_points=[CommandHandler("allclear", cmd_allclear_start)],
        states={ALLCLEAR_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_allclear_confirm)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="allclear_conv",
        persistent=False,
    )
    app.add_handler(allclear_conv)

    # Admin quick add: use /addav /addvoca /addgram /addquiz to open template (optional shortcut)
    addav = ConversationHandler(
        entry_points=[CommandHandler("addav", lambda u, c: admin_add_content_template(u, c, "AV"))],
        states={BC_WAIT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_parse_and_save)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    addvoca = ConversationHandler(
        entry_points=[CommandHandler("addvoca", lambda u, c: admin_add_content_template(u, c, "VOCA"))],
        states={BC_WAIT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_parse_and_save)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    addgram = ConversationHandler(
        entry_points=[CommandHandler("addgram", lambda u, c: admin_add_content_template(u, c, "GRAM"))],
        states={BC_WAIT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_parse_and_save)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    addquiz = ConversationHandler(
        entry_points=[CommandHandler("addquiz", lambda u, c: admin_add_content_template(u, c, "QUIZ"))],
        states={BC_WAIT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_parse_and_save)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(addav)
    app.add_handler(addvoca)
    app.add_handler(addgram)
    app.add_handler(addquiz)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
