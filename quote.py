"""
    name: quote
    description: Стикер-цитата из сообщений, как у @QuotLyBot
    usage (ответом на сообщение):
        .q                — стикер из одного сообщения
        .q 3              — из этого и 2 следующих сообщений
        .q #232323        — свой цвет фона (hex или название, напр. black)
        .q 3 black        — можно комбинировать
        .q bot            — сделать через самого @QuotLyBot (если API недоступен)
"""

import asyncio
import base64
import io
import json
import re
import urllib.request

import tgpy.api
from telethon import types

# ─── Настройки ────────────────────────────────────────────────────────────────
QUOTE_API_URL = "https://bot.lyo.su/quote/generate"   # публичный API QuotLy
QUOTE_BG_COLOR = "#1b1429"
QUOTE_MAX_MESSAGES = 10
QUOTE_DELETE_CMD = True        # удалять сообщение с командой после отправки
QUOTE_BOT = "QuotLyBot"
# ──────────────────────────────────────────────────────────────────────────────

_ENTITY_TYPES = {
    types.MessageEntityBold: "bold",
    types.MessageEntityItalic: "italic",
    types.MessageEntityUnderline: "underline",
    types.MessageEntityStrike: "strikethrough",
    types.MessageEntityCode: "code",
    types.MessageEntityPre: "pre",
    types.MessageEntitySpoiler: "spoiler",
    types.MessageEntityUrl: "url",
    types.MessageEntityTextUrl: "text_link",
    types.MessageEntityMention: "mention",
    types.MessageEntityMentionName: "text_mention",
    types.MessageEntityHashtag: "hashtag",
    types.MessageEntityCashtag: "cashtag",
    types.MessageEntityBotCommand: "bot_command",
    types.MessageEntityEmail: "email",
    types.MessageEntityPhone: "phone_number",
}


def _entities(msg) -> list[dict]:
    out = []
    for e in msg.entities or []:
        t = _ENTITY_TYPES.get(type(e))
        if t:
            out.append({"type": t, "offset": e.offset, "length": e.length})
    return out


def _author(msg) -> dict:
    s = msg.sender
    fwd = msg.fwd_from
    if fwd and fwd.from_name:  # переслано от скрытого пользователя
        return {"id": abs(hash(fwd.from_name)) % 10**9, "name": fwd.from_name}
    if s is None:
        return {"id": msg.sender_id or 0, "name": "Unknown"}
    name = getattr(s, "title", None) or " ".join(
        x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x
    )
    return {
        "id": s.id,
        "name": name or getattr(s, "username", None) or str(s.id),
        "username": getattr(s, "username", None),
    }


def _text(msg) -> str:
    text = msg.message or ""
    if msg.sticker:
        return text or "[стикер]"
    if msg.photo:
        return text or "[фото]"
    if msg.media and not text:
        return "[медиа]"
    return text


def _generate_sync(payload: dict) -> bytes:
    req = urllib.request.Request(
        QUOTE_API_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if not data.get("ok"):
        raise RuntimeError(data)
    return base64.b64decode(data["result"]["image"])


async def _collect(reply, count: int) -> list:
    msgs = [reply]
    if count > 1:
        async for m in client.iter_messages(
            reply.chat_id, min_id=reply.id, reverse=True, limit=count - 1
        ):
            msgs.append(m)
    return msgs


async def _via_bot(msgs, chat_id, reply_to):
    async with client.conversation(QUOTE_BOT, timeout=60) as conv:
        await client.forward_messages(QUOTE_BOT, msgs)
        resp = await conv.get_response()
    await client.send_file(chat_id, resp.media, reply_to=reply_to)


async def _delete_later(msg, delay=1.5):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def quote(args: str = ""):
    msg = ctx.msg
    reply = await msg.get_reply_message()
    if reply is None:
        return "Ответь командой на сообщение"

    count, color, use_bot = 1, QUOTE_BG_COLOR, False
    for a in str(args).split():
        if a.isdigit():
            count = max(1, min(int(a), QUOTE_MAX_MESSAGES))
        elif a.lower() == "bot":
            use_bot = True
        else:
            color = a

    msgs = await _collect(reply, count)
    msgs = [m for m in msgs if m.id != msg.id]

    try:
        if use_bot:
            await _via_bot(msgs, msg.chat_id, reply.id)
        else:
            payload = {
                "type": "quote",
                "format": "webp",
                "backgroundColor": color,
                "width": 512,
                "height": 768,
                "scale": 2,
                "messages": [
                    {
                        "entities": _entities(m),
                        "avatar": True,
                        "from": _author(m),
                        "text": _text(m),
                        "replyMessage": {},
                    }
                    for m in msgs
                ],
            }
            image = await asyncio.to_thread(_generate_sync, payload)
            file = io.BytesIO(image)
            file.name = "quote.webp"
            await client.send_file(
                msg.chat_id,
                file,
                reply_to=reply.id,
                force_document=True,
                mime_type="image/webp",
                attributes=[
                    types.DocumentAttributeSticker(
                        alt="💬", stickerset=types.InputStickerSetEmpty()
                    ),
                    types.DocumentAttributeFilename("quote.webp"),
                ],
            )
    except Exception as e:
        return f"Ошибка: {e!r}"

    if QUOTE_DELETE_CMD:
        asyncio.create_task(_delete_later(msg))
    return "✅"


def _quote_transformer(code: str) -> str:
    m = re.fullmatch(r"\s*[./]q(?:uote)?(?:\s+(.*))?", code, re.S | re.I)
    if not m:
        return code
    return f"await quote({(m[1] or '').strip()!r})"


tgpy.api.code_transformers.add("quote", _quote_transformer)
