"""
    name: recap
    description: Рекап чата через нейросеть (OpenAI-совместимый API)
    usage:
        .recap 100          — последние 100 сообщений
        .recap 29.09        — с 29 сентября (текущего года) до сейчас
        .recap 29.09.2026   — с указанной даты
        .recap 2026-09-29   — то же, ISO-формат
        .recap 29.09 30.09  — диапазон дат (включительно)
        .recap сегодня / вчера
        .recap 3h / 2d      — за последние 3 часа / 2 дня
"""

import asyncio
import json
import re
import urllib.request
from datetime import datetime, timedelta

import tgpy.api

# ─── Настройки ────────────────────────────────────────────────────────────────
# Любой OpenAI-совместимый API: OpenAI, OpenRouter, DeepSeek, Groq, LM Studio...
RECAP_API_KEY = "sk-..."
RECAP_API_URL = "https://api.openai.com/v1/chat/completions"
RECAP_MODEL = "gpt-4o-mini"

RECAP_PROMPT = "Рекап"            # слово/инструкция, с которой уходят сообщения
RECAP_SYSTEM = (
    "Ты делаешь краткий рекап переписки из Telegram-чата на русском языке. "
    "Выдели основные темы, ключевые решения, вопросы и кто что предлагал. "
    "Пиши структурированно и по делу."
)
RECAP_MAX_MESSAGES = 3000         # потолок сообщений за один рекап
RECAP_MAX_CHARS = 120_000         # потолок символов, отправляемых в нейросеть
RECAP_TIMEOUT = 180               # секунд на ответ API
# ──────────────────────────────────────────────────────────────────────────────

_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _parse_date(s: str) -> datetime | None:
    s = s.strip().lower()
    today = datetime.now(_LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("today", "сегодня"):
        return today
    if s in ("yesterday", "вчера"):
        return today - timedelta(days=1)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_LOCAL_TZ)
        except ValueError:
            pass
    try:
        d = datetime.strptime(s, "%d.%m")
        return d.replace(year=today.year, tzinfo=_LOCAL_TZ)
    except ValueError:
        return None


def _parse_args(args: str):
    """Возвращает (limit, since, until). Ровно одно из limit/since задано."""
    parts = args.split()
    if not parts:
        raise ValueError("укажи количество сообщений или дату")

    if len(parts) == 1 and parts[0].isdigit():
        return min(int(parts[0]), RECAP_MAX_MESSAGES), None, None

    m = re.fullmatch(r"(\d+)\s*([hdчд])", parts[0].lower())
    if len(parts) == 1 and m:
        n, unit = int(m[1]), m[2]
        delta = timedelta(hours=n) if unit in "hч" else timedelta(days=n)
        return None, datetime.now(_LOCAL_TZ) - delta, None

    since = _parse_date(parts[0])
    if since is None:
        raise ValueError(f"не понял дату: {parts[0]}")
    until = None
    if len(parts) > 1:
        until = _parse_date(parts[1])
        if until is None:
            raise ValueError(f"не понял дату: {parts[1]}")
        until += timedelta(days=1)  # включительно
    return None, since, until


def _sender_name(msg) -> str:
    s = msg.sender
    if s is None:
        return "?"
    name = getattr(s, "title", None) or " ".join(
        x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x
    )
    return name or getattr(s, "username", None) or str(msg.sender_id)


def _format(msgs) -> str:
    lines = []
    for m in msgs:
        text = m.message or ""
        if m.media and not text:
            text = "[медиа]"
        elif m.media:
            text = "[медиа] " + text
        if not text:
            continue
        t = m.date.astimezone(_LOCAL_TZ).strftime("%d.%m %H:%M")
        lines.append(f"[{t}] {_sender_name(m)}: {text}")
    out = "\n".join(lines)
    if len(out) > RECAP_MAX_CHARS:
        out = out[-RECAP_MAX_CHARS:]  # оставляем самые свежие
    return out


def _ask_llm_sync(chat_text: str) -> str:
    body = json.dumps({
        "model": RECAP_MODEL,
        "messages": [
            {"role": "system", "content": RECAP_SYSTEM},
            {"role": "user", "content": f"{RECAP_PROMPT}\n\n{chat_text}"},
        ],
    }).encode()
    req = urllib.request.Request(
        RECAP_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RECAP_API_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=RECAP_TIMEOUT) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


async def recap(args: str = "100"):
    msg = ctx.msg
    limit, since, until = _parse_args(str(args))

    if limit is not None:
        msgs = [
            m async for m in client.iter_messages(msg.chat_id, limit=limit, max_id=msg.id)
        ]
        msgs.reverse()
    else:
        msgs = []
        async for m in client.iter_messages(
            msg.chat_id, offset_date=since, reverse=True
        ):
            if m.id >= msg.id or (until and m.date >= until):
                break
            msgs.append(m)
            if len(msgs) >= RECAP_MAX_MESSAGES:
                break

    chat_text = _format(msgs)
    if not chat_text:
        return "Нет сообщений для рекапа"

    try:
        answer = await asyncio.to_thread(_ask_llm_sync, chat_text)
    except Exception as e:
        return f"Ошибка API: {e!r}"

    text = f"📝 Рекап ({len(msgs)} сообщ.):\n\n{answer}"
    for i in range(0, len(text), 4096):
        await client.send_message(msg.chat_id, text[i:i + 4096])
    return f"✅ Рекап отправлен ({len(msgs)} сообщ.)"


def _recap_transformer(code: str) -> str:
    m = re.fullmatch(r"\s*[./]recap(?:\s+(.*))?", code, re.S | re.I)
    if not m:
        return code
    return f"await recap({(m[1] or '100').strip()!r})"


tgpy.api.code_transformers.add("recap", _recap_transformer)
