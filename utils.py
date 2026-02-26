from typing import Optional
from telegram import User


def is_admin(user_id: int, admin_ids: set[int]) -> bool:
    return user_id in admin_ids


def safe_name(u: User) -> str:
    if u.username:
        return f"@{u.username}"
    return (u.full_name or "User").strip()


def parse_type_token(token: str) -> Optional[str]:
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


def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))
