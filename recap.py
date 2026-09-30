"""
    name: recap
    origin: https://github.com/com-master/tgpy-scripts/recap.py
    priority: 1000
    description: Рекап чата через нейросеть (OpenAI-совместимый API)
"""

# usage:
#     .recap 100          — последние 100 сообщений
#     .recap 29.09        — с 29 сентября (текущего года) до сейчас
#     .recap 29.09.2026   — с указанной даты
#     .recap 2026-09-29   — то же, ISO-формат
#     .recap 29.09 30.09  — диапазон дат (включительно)
#     .recap сегодня / вчера
#     .recap 3h / 2d      — за последние 3 часа / 2 дня
#
# Другой чат — добавь его id / @username / ссылку (в любом месте):
#     .recap 100 -1001234567890
#     .recap 29.09 @durov
#     .recap 2d https://t.me/some_chat
#     .recap 50 id:123456789  — положительный id (личка/бот) через id:
# Рекап приходит в чат, где введена команда.
#
# Комментарий к слову «Рекап» — в конце команды:
#     .recap 100 коротко        — пресеты: коротко / подробно / тезисы
#     .recap 2d только про деньги и сроки
#     .recap подробно           — без числа/даты берутся 100 сообщений

import asyncio
import html as html_lib
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
    "Пиши структурированно и по делу. Оформляй ответ в Markdown: "
    "заголовки ###, списки через «- », **жирный**, *курсив*."
)
# Пресеты комментария: `.recap 100 коротко` → «Рекап: <текст пресета>».
# Любой другой текст после количества/даты уходит к слову «Рекап» как есть.
RECAP_STYLES = {
    ("коротко", "кратко", "short"): "коротко, 3–5 главных пунктов, без деталей",
    ("подробно", "детально", "full"): "подробно: все темы, решения, кто что сказал",
    ("тезисы", "пункты"): "только список тезисов, без вступления и выводов",
}
RECAP_SPLIT = 3800                # макс. длина одной части ответа (лимит TG 4096)
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


def _extract_chat(parts: list[str]):
    """Вынимает из аргументов ссылку на чат. Возвращает (chat | None, остальное)."""
    chat, rest = None, []
    for p in parts:
        low = p.lower()
        if chat is None and re.fullmatch(r"-\d+", p):
            chat = int(p)
        elif chat is None and low.startswith(("id:", "chat:")):
            v = p.split(":", 1)[1]
            chat = int(v) if re.fullmatch(r"-?\d+", v) else v
        elif chat is None and (p.startswith("@") or "t.me/" in low):
            chat = p
        else:
            rest.append(p)
    return chat, rest


def _looks_like_date(s: str) -> bool:
    return bool(re.fullmatch(r"[\d.\-]+", s)) and not s.isdigit()


def _parse_args(parts: list[str]):
    """Возвращает (limit, since, until, comment). Ровно одно из limit/since задано.
    Всё, что после количества/даты, — комментарий к слову «Рекап»."""
    first = parts[0] if parts else ""
    rest = parts[1:]
    limit = since = until = None
    m = re.fullmatch(r"(\d+)\s*([hdчд])", first.lower())

    if first.isdigit():
        limit = min(int(first), RECAP_MAX_MESSAGES)
    elif m:
        n, unit = int(m[1]), m[2]
        delta = timedelta(hours=n) if unit in "hч" else timedelta(days=n)
        since = datetime.now(_LOCAL_TZ) - delta
    elif (since := _parse_date(first)) is not None:
        if rest and (until := _parse_date(rest[0])) is not None:
            until += timedelta(days=1)  # включительно
            rest = rest[1:]
        elif rest and _looks_like_date(rest[0]):
            raise ValueError(f"не понял дату: {rest[0]}")
    elif _looks_like_date(first):
        raise ValueError(f"не понял дату: {first}")
    else:
        limit, rest = 100, parts  # `.recap подробно` — 100 сообщений

    return limit, since, until, " ".join(rest)


def _style(comment: str) -> str:
    low = comment.strip().lower()
    for keys, text in RECAP_STYLES.items():
        if low in keys:
            return text
    return comment.strip()


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


def _ask_llm_sync(chat_text: str, comment: str = "") -> str:
    prompt = f"{RECAP_PROMPT}: {comment}" if comment else RECAP_PROMPT
    body = json.dumps({
        "model": RECAP_MODEL,
        "messages": [
            {"role": "system", "content": RECAP_SYSTEM},
            {"role": "user", "content": f"{prompt}\n\n{chat_text}"},
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


def _split(text: str, size: int) -> list[str]:
    """Режем по строкам, чтобы не разрывать разметку посреди строки."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:  # очень длинная строка
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:size])
            line = line[size:]
        if cur and len(cur) + len(line) + 1 > size:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _md_inline(s: str) -> str:
    s = html_lib.escape(s, quote=False)
    codes = []

    def keep_code(m):
        codes.append(m[1])
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", keep_code, s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)", r"<i>\1</i>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m[1])]}</code>", s)


def _md_to_html(text: str) -> str:
    """Markdown от нейросети → HTML, который понимает Telegram."""
    out, in_code, code_buf = [], False, []
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre>" + html_lib.escape("\n".join(code_buf), quote=False) + "</pre>")
                code_buf = []
            in_code = not in_code
            continue
        if in_code:
            code_buf.append(line)
            continue

        if m := re.match(r"\s*#{1,6}\s+(.*)", line):  # заголовок
            title = re.sub(r"^\*\*(.*)\*\*$", r"\1", m[1].strip())
            out.append(f"<b>{_md_inline(title)}</b>")
        elif re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", line):  # --- разделитель
            out.append("──────────")
        elif m := re.match(r"(\s*)[*\-+]\s+(.*)", line):  # список
            level = min((len(m[1].expandtabs(4)) + 2) // 4, 3)
            bullet = "•" if level == 0 else "◦"
            out.append("    " * level + f"{bullet} {_md_inline(m[2])}")
        elif m := re.match(r"\s*>\s?(.*)", line):  # цитата
            out.append(f"<blockquote>{_md_inline(m[1])}</blockquote>")
        else:
            out.append(_md_inline(line))
    if in_code and code_buf:
        out.append("<pre>" + html_lib.escape("\n".join(code_buf), quote=False) + "</pre>")
    return "\n".join(out)


async def _set_status(msg, text: str):
    """Сами пишем статус в сообщение с командой, TGPy его не трогает."""
    try:
        await msg.edit(text)
    except Exception:  # MessageNotModified / сообщение удалено и т.п.
        pass


async def recap(args: str = "100"):
    msg = ctx.msg
    if msg is not None:
        ctx.is_manual_output = True  # иначе TGPy сам редактирует сообщение
    status = await _recap(msg, args)
    if msg is not None:
        await _set_status(msg, status)
    return status


async def _recap(msg, args):
    chat_ref, parts = _extract_chat(str(args).split())
    try:
        limit, since, until, comment = _parse_args(parts)
    except ValueError as e:
        return f"❌ {e}"

    if chat_ref is None:
        chat, max_id = msg.chat_id, msg.id  # не берём саму команду
    else:
        try:
            chat = await client.get_entity(chat_ref)
        except Exception as e:
            return f"❌ Не нашёл чат {chat_ref}: {e!r}"
        max_id = 0

    if limit is not None:
        msgs = [
            m async for m in client.iter_messages(chat, limit=limit, max_id=max_id)
        ]
        msgs.reverse()
    else:
        msgs = []
        async for m in client.iter_messages(chat, offset_date=since, reverse=True):
            if (max_id and m.id >= max_id) or (until and m.date >= until):
                break
            msgs.append(m)
            if len(msgs) >= RECAP_MAX_MESSAGES:
                break

    chat_text = _format(msgs)
    if not chat_text:
        return "❌ Нет сообщений для рекапа"

    try:
        answer = await asyncio.to_thread(_ask_llm_sync, chat_text, _style(comment))
    except Exception as e:
        return f"❌ Ошибка API: {e!r}"

    text = f"📝 **Рекап** ({len(msgs)} сообщ.)\n\n{answer}"
    for chunk in _split(text, RECAP_SPLIT):
        try:
            await client.send_message(msg.chat_id, _md_to_html(chunk), parse_mode="html")
        except Exception:  # на случай кривой разметки — шлём как есть
            await client.send_message(msg.chat_id, chunk, parse_mode=None)
    return f"✅ Рекап отправлен ({len(msgs)} сообщ.)"


def _recap_transformer(code: str) -> str:
    m = re.fullmatch(r"\s*[./]recap(?:\s+(.*))?", code, re.S | re.I)
    if not m:
        return code
    return f"await recap({(m[1] or '100').strip()!r})"


tgpy.api.code_transformers.add("recap", _recap_transformer)
