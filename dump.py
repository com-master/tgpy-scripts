"""
    name: dump
    origin: https://github.com/com-master/tgpy-scripts/dump.py
    priority: 1003
    description: Полный дамп чата (TXT + JSON, по желанию с медиа) в zip-архив
"""

# usage:
#     .dump -1001234567890            — весь чат: messages.txt + messages.jsonl
#     .dump -1001234567890 media      — то же + все медиафайлы
#     .dump @chat 90d                 — только за последние 90 дней
#     .dump @chat 01.09.2026 до:30.09.2026   — диапазон дат (конец включительно)
#     .dump @chat 5000                — последние 5000 сообщений
#     .dump                           — текущий чат
#
# Чат: -100…, @username, ссылка t.me/…, id:123 (личка/бот). Без чата — текущий.
# Срок/дата начала: 12h, 90d, 2w, 6mo, 1y, 29.09, 29.09.2026, 2026-09-29, вчера.
# Конец: до:<дата> / to:<дата> (включительно).
# Архив приходит файлом в чат, где введена команда (лучше «Избранное»),
# и остаётся на сервере в папке dumps/ рядом с модулями TGPy.

import asyncio
import json
import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import tgpy.api
from telethon import errors, types, utils

# ─── Настройки ────────────────────────────────────────────────────────────────
DUMP_DIR = Path(os.environ.get("TGPY_DATA") or Path.home() / ".config" / "tgpy") / "dumps"
DUMP_MEDIA_MAX_MB = 200        # медиа крупнее пропускаются (0 — без лимита)
DUMP_SEND_MAX_MB = 1900        # архив больше — не отправляется, только путь на сервере
DUMP_PROGRESS_EVERY = 3        # секунд между обновлениями статуса
DUMP_KEEP_FOLDER = False       # оставлять ли распакованную папку рядом с zip
# ──────────────────────────────────────────────────────────────────────────────

_DUMP_TZ = datetime.now().astimezone().tzinfo
_DUMP_UNITS = {
    "h": timedelta(hours=1), "ч": timedelta(hours=1),
    "d": timedelta(days=1), "д": timedelta(days=1),
    "w": timedelta(weeks=1), "н": timedelta(weeks=1),
    "mo": timedelta(days=30), "мес": timedelta(days=30),
    "y": timedelta(days=365), "г": timedelta(days=365),
}
_DUMP_UNTIL = ("до:", "to:", "until:", "по:")
_DUMP_SINCE = ("с:", "from:", "since:")


def _dump_date(s: str, end=False):
    """Срок или дата → aware datetime. end=True — конец указанного дня."""
    s = s.strip().lower()
    now = datetime.now(_DUMP_TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.fullmatch(r"(\d+)\s*([a-zа-я]+)", s)
    if m and m[2] in _DUMP_UNITS:
        return now - int(m[1]) * _DUMP_UNITS[m[2]]
    d = None
    if s in ("today", "сегодня"):
        d = today
    elif s in ("yesterday", "вчера"):
        d = today - timedelta(days=1)
    else:
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
            try:
                d = datetime.strptime(s, fmt).replace(tzinfo=_DUMP_TZ)
                break
            except ValueError:
                pass
        else:
            try:
                d = datetime.strptime(s, "%d.%m").replace(year=today.year, tzinfo=_DUMP_TZ)
            except ValueError:
                return None
    return d + timedelta(days=1) if end else d


def _dump_ref(v: str):
    return int(v) if re.fullmatch(r"-?\d+", v) else v


def _dump_parse(args: str):
    chat, since, until, limit, media = None, None, None, None, False
    for p in str(args).split():
        low = p.lower()
        pre_u = next((x for x in _DUMP_UNTIL if low.startswith(x)), None)
        pre_s = next((x for x in _DUMP_SINCE if low.startswith(x)), None)
        if low in ("media", "медиа", "files", "файлы"):
            media = True
        elif pre_u:
            until = _dump_date(p[len(pre_u):], end=True)
            if until is None:
                raise ValueError(f"не понял дату: {p}")
        elif pre_s or (not low.startswith("-") and _dump_date(low) is not None):
            since = _dump_date(p[len(pre_s):] if pre_s else p)
            if since is None:
                raise ValueError(f"не понял дату: {p}")
        elif p.isdigit():
            limit = int(p)
        elif chat is None:
            chat = _dump_ref(p.split(":", 1)[1] if low.startswith(("id:", "chat:")) else p)
        else:
            raise ValueError(f"лишний аргумент: {p}")
    if since and until and since >= until:
        raise ValueError("начало позже конца")
    return chat, since, until, limit, media


def _dump_json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.hex()
    return repr(o)


def _dump_name(e) -> str:
    if e is None:
        return "?"
    if getattr(e, "title", None):
        return e.title
    name = " ".join(x for x in (getattr(e, "first_name", None),
                                getattr(e, "last_name", None)) if x)
    return name or getattr(e, "username", None) or str(e.id)


def _dump_media_label(m) -> str:
    if m.photo:
        return "фото"
    if m.sticker:
        alt = next((a.alt for a in m.sticker.attributes
                    if isinstance(a, types.DocumentAttributeSticker)), "")
        return f"стикер {alt}".strip()
    if m.voice:
        return "голосовое"
    if m.video_note:
        return "кружок"
    if m.gif:
        return "GIF"
    if m.video:
        return "видео"
    if m.audio:
        return "аудио"
    if m.poll:
        return "опрос: " + str(getattr(m.poll.poll.question, "text", m.poll.poll.question))
    if m.geo:
        return f"гео {m.geo.lat:.5f},{m.geo.long:.5f}"
    if m.contact:
        return f"контакт {m.contact.first_name} {m.contact.phone_number}"
    if m.document:
        return "файл " + (m.file.name or "")
    if m.web_preview:
        return ""
    return type(m.media).__name__ if m.media else ""


def _dump_size(m) -> int:
    return (getattr(m.file, "size", 0) or 0) if m.file else 0


async def _dump_call(fn, *args, **kwargs):
    while True:
        try:
            return await fn(*args, **kwargs)
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)


async def _dump_topics(chat) -> dict:
    """id топика → название (для форумов)."""
    try:
        try:
            from telethon.tl.functions.messages import GetForumTopicsRequest
            req = lambda **kw: GetForumTopicsRequest(peer=chat, **kw)  # noqa: E731
        except ImportError:
            from telethon.tl.functions.channels import GetForumTopicsRequest
            req = lambda **kw: GetForumTopicsRequest(channel=chat, **kw)  # noqa: E731
        names, off_date, off_id, off_topic = {}, None, 0, 0
        while True:
            res = await _dump_call(client, req(offset_date=off_date, offset_id=off_id,
                                               offset_topic=off_topic, limit=100))
            new = [t for t in res.topics
                   if isinstance(t, types.ForumTopic) and t.id not in names]
            for t in new:
                names[t.id] = t.title
            if not new or len(names) >= res.count:
                return names
            last = res.topics[-1]
            off_date, off_id, off_topic = last.date, last.top_message, last.id
    except Exception:
        return {}


def _dump_topic_id(m, topics=()):
    if m.id in topics:  # сообщение-создание топика
        return m.id
    r = m.reply_to
    if not r or not getattr(r, "forum_topic", False):
        return 1  # «Общий»
    return r.reply_to_top_id or r.reply_to_msg_id


async def dump(args: str = ""):
    msg = ctx.msg
    ctx.is_manual_output = True
    try:
        status = await _dump_run(msg, args)
    except ValueError as e:
        status = f"❌ {e}"
    except Exception as e:
        status = f"❌ Ошибка: {e!r}"
    try:
        await msg.edit(status)
    except Exception:
        pass
    return status


async def _dump_run(msg, args):
    chat_ref, since, until, limit, media = _dump_parse(args)
    try:
        chat = await client.get_entity(chat_ref if chat_ref is not None else msg.chat_id)
    except Exception as e:
        return f"❌ Не нашёл чат {chat_ref}: {e!r}"

    title = _dump_name(chat)
    chat_id = utils.get_peer_id(chat)
    is_forum = bool(getattr(chat, "forum", False))
    topics = await _dump_topics(chat) if is_forum else {}

    stamp = datetime.now(_DUMP_TZ).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-]+", "_", title, flags=re.U).strip("_")[:40] or "chat"
    folder = DUMP_DIR / f"{safe}_{chat_id}_{stamp}"
    media_dir = folder / "media"
    folder.mkdir(parents=True, exist_ok=True)
    if media:
        media_dir.mkdir(exist_ok=True)

    # Итерируем от старых к новым. С началом — первые N после него;
    # только с лимитом — последние N (до конца диапазона), развернём ниже.
    last_n = bool(limit and not since)
    if last_n:
        kw = {"limit": limit, "offset_date": until}
    else:
        kw = {"reverse": True, "offset_date": since, "limit": limit}
    skip_id = msg.id if msg.chat_id == chat_id else None

    count = media_count = media_skipped = 0
    senders: dict = {}
    last_edit = 0.0
    max_bytes = DUMP_MEDIA_MAX_MB * 1024 * 1024

    async def progress(force=False):
        nonlocal last_edit
        if not force and time.monotonic() - last_edit < DUMP_PROGRESS_EVERY:
            return
        last_edit = time.monotonic()
        try:
            await msg.edit(f"📦 Дамп «{title}»…\nСообщений: {count}"
                           + (f"\nМедиа: {media_count}" if media else ""))
        except Exception:
            pass

    await progress(force=True)
    messages = client.iter_messages(chat, **kw)
    if last_n:  # нужно от старых к новым — собираем и разворачиваем
        messages = list(reversed([m async for m in messages]))

    with open(folder / "messages.txt", "w", encoding="utf-8") as txt, \
         open(folder / "messages.jsonl", "w", encoding="utf-8") as js:
        txt.write(f"Чат: {title} (id {chat_id})\n"
                  f"Выгружено: {datetime.now(_DUMP_TZ):%d.%m.%Y %H:%M}\n\n")

        async def handle(m):
            nonlocal count, media_count, media_skipped
            if m.id == skip_id or (until and m.date >= until):
                return
            sid = m.sender_id
            if sid not in senders:
                senders[sid] = _dump_name(m.sender) if m.sender else (
                    title if m.post else str(sid))
            rec = m.to_dict()
            rec["_sender_name"] = senders[sid]

            parts = []
            if m.media and not m.web_preview:
                label = _dump_media_label(m)
                if media:
                    if max_bytes and _dump_size(m) > max_bytes:
                        media_skipped += 1
                        label += f" (пропущено: {_dump_size(m) // 2**20} МБ)"
                    else:
                        try:
                            fname = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_",
                                           m.file.name or "")[:80]
                            path = await _dump_call(
                                client.download_media, m,
                                file=str(media_dir / (f"{m.id}_{fname}" if fname else str(m.id))))
                            if path:
                                rel = os.path.relpath(path, folder)
                                rec["_media_file"] = rel
                                label += f" → {rel}"
                                media_count += 1
                        except Exception as e:
                            label += f" (ошибка загрузки: {e.__class__.__name__})"
                if label:
                    parts.append(f"[{label}]")
            if m.action:
                parts.append(f"[служебное: {type(m.action).__name__}]")
            if m.text:
                parts.append(m.raw_text)

            head = f"[{m.date.astimezone(_DUMP_TZ):%d.%m.%Y %H:%M}] #{m.id} {senders[sid]}"
            if is_forum:
                head += f" [{topics.get(_dump_topic_id(m, topics), 'топик ' + str(_dump_topic_id(m, topics)))}]"
            if m.fwd_from:
                head += " (переслано)"
            if m.reply_to and m.reply_to.reply_to_msg_id and not (
                    is_forum and m.reply_to.reply_to_msg_id == _dump_topic_id(m, topics)):
                head += f" ↩#{m.reply_to.reply_to_msg_id}"
            if m.edit_date:
                head += " (изм.)"
            txt.write(head + ":\n" + ("\n".join(parts) or "—") + "\n\n")
            js.write(json.dumps(rec, ensure_ascii=False, default=_dump_json_default) + "\n")
            count += 1
            await progress()

        if last_n:
            for m in messages:
                await handle(m)
        else:
            async for m in messages:
                if until and m.date >= until:
                    break  # дальше только новее
                await handle(m)

    with open(folder / "chat.json", "w", encoding="utf-8") as f:
        json.dump({"chat": chat.to_dict(), "topics": topics, "senders": senders,
                   "since": since, "until": until, "limit": limit, "messages": count},
                  f, ensure_ascii=False, indent=1, default=_dump_json_default)

    await progress(force=True)
    zip_path = folder.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for p in folder.rglob("*"):
            if p.is_file():
                # медиа уже сжаты — не тратим время на повторное сжатие
                z.write(p, p.relative_to(folder),
                        zipfile.ZIP_STORED if p.parent.name == "media" else None)
    if not DUMP_KEEP_FOLDER:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)

    size_mb = zip_path.stat().st_size / 2**20
    info = [f"✅ «{title}»: сообщений {count}"]
    if media:
        info.append(f"Медиа: {media_count}"
                    + (f", пропущено крупных: {media_skipped}" if media_skipped else ""))
    info.append(f"Архив: {size_mb:.1f} МБ")
    if size_mb > DUMP_SEND_MAX_MB:
        info.append(f"Слишком большой для отправки, лежит на сервере:\n{zip_path}")
        return "\n".join(info)
    await client.send_file(msg.chat_id, str(zip_path), caption="\n".join(info),
                           force_document=True)
    return "\n".join(info) + "\n(архив отправлен ниже)"


def _dump_transformer(code: str) -> str:
    m = re.fullmatch(r"\s*[./]dump(?:\s+(.*))?", code, re.S | re.I)
    if not m:
        return code
    return f"await dump({(m[1] or '').strip()!r})"


tgpy.api.code_transformers.add("dump", _dump_transformer)
