from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Optional


LANGS = [
    ("English", "en"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Chinese", "zh"),
    ("Thai", "th"),
    ("Myanmar", "my"),
]


def lang_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for name, code in LANGS:
        rows.append([InlineKeyboardButton(name, callback_data=f"lang:{code}")])
    return InlineKeyboardMarkup(rows)


def section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("A&V (ဗျည်း/သရ/ပေါင်းသံ)", callback_data="sec:AV")],
            [InlineKeyboardButton("Voca (ဝေါဟာရ)", callback_data="sec:VOCA")],
            [InlineKeyboardButton("Gram (သဒ္ဒါ)", callback_data="sec:GRAM")],
        ]
    )


def difficulty_keyboard(current: Optional[str] = None) -> InlineKeyboardMarkup:
    def btn(label: str) -> InlineKeyboardButton:
        suffix = " ✅" if current == label else ""
        return InlineKeyboardButton(label + suffix, callback_data=f"diff:{label}")

    return InlineKeyboardMarkup(
        [
            [btn("Basic"), btn("Medium"), btn("Hard")],
        ]
    )


def content_list_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for it in items[:30]:
        key = it.get("key", "Item")
        level_tag = it.get("level_tag")
        label = f"{key}" + (f" ({level_tag})" if level_tag else "")
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"content:{it['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back:home")])
    return InlineKeyboardMarkup(rows)


def quiz_keyboard(qid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("A", callback_data=f"q:{qid}:A"),
                InlineKeyboardButton("B", callback_data=f"q:{qid}:B"),
            ],
            [
                InlineKeyboardButton("C", callback_data=f"q:{qid}:C"),
                InlineKeyboardButton("D", callback_data=f"q:{qid}:D"),
            ],
        ]
    )


def admin_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Choose Language", callback_data="admin:choose_lang")],
            [InlineKeyboardButton("Add/Update A&V", callback_data="admin:add:AV")],
            [InlineKeyboardButton("Add/Update Voca", callback_data="admin:add:VOCA")],
            [InlineKeyboardButton("Add/Update Gram", callback_data="admin:add:GRAM")],
            [InlineKeyboardButton("Add Quiz (template)", callback_data="admin:add:QUIZ")],
            [InlineKeyboardButton("⬅️ Exit", callback_data="admin:exit")],
        ]
    )
