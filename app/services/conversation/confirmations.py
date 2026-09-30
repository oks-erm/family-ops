"""Confirmation help exposes only application-owned, still-active proposals."""

import re
from datetime import UTC, datetime


def confirmation_help(text):
    normalized = re.sub(r"[^a-z ]", " ", text.casefold())
    normalized = " ".join(normalized.split())
    # Exact, complete help utterances only; compound/new requests still need understanding.
    return normalized in {
        "output the exact reply so i could copy it",
        "output the exact reply so i can copy it",
        "give me the exact reply",
        "give me the confirmation code",
        "what do i reply",
        "what should i reply",
        "copyable confirmation",
        "show confirmation",
        "confirmation code",
        "repeat the confirmation code",
        "how do i confirm",
        "what should i type",
    }


def pending_reply(pending, format="code"):
    if not pending or datetime.fromisoformat(pending["expires_at"]) <= datetime.now(UTC):
        return "There is no active confirmation. Please request the change again."
    code = f"confirm {pending['token']}"
    if format == "code":
        return code
    return f"{pending['description']}\n\n{code}\n\nOr cancel. The original expiry still applies."


def confirmation_buttons(text):
    # Only application-generated, standalone confirmation commands produce buttons.
    matches = re.findall(r"^confirm ([0-9a-f]{8})$", text, flags=re.M)
    if len(matches) != 1:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    token = matches[0]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Confirm change", callback_data=f"assistant:confirm:{token}"
                ),
                InlineKeyboardButton(text="Cancel", callback_data=f"assistant:cancel:{token}"),
            ]
        ]
    )


def confirmation_callback(payload):
    callback = payload.get("callback_query") or {}
    match = re.fullmatch(r"assistant:(confirm|cancel):([0-9a-f]{8})", callback.get("data", ""))
    if match and (callback.get("message") or {}).get("chat", {}).get("type") == "private":
        return f"{match[1]} {match[2]}"
    return None
