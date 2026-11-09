"""
    name: dump
    origin: https://github.com/com-master/tgpy-scripts/dump.py
    priority: 1003
    description: Полный дамп чата (TXT + JSON, по желанию с медиа) в zip-архив
"""

# usage:
#     .dump -1001234567890            — весь чат: messages.txt + messages.jsonl
#     .dump -1001234567890 media      — то же + все медиафайлы (до DUMP_MEDIA_MAX_MB)
#     .dump @chat media:50            — медиа, но каждый файл не больше 50 МБ
#     .dump @chat media:photo,video   — только фото и видео
#     .dump @chat media:photo,voice,20 — фото и голосовые до 20 МБ
#     .dump @chat media part:500mb    — архив частями по 500 МБ
#     .dump @chat 90d                 — только за последние 90 дней
#     .dump @chat 01.09.2026 до:30.09.2026   — диапазон дат (конец включительно)
#     .dump @chat 5000                — последние 5000 сообщений
#     .dump                           — текущий чат
#
# Чат: -100…, @username, ссылка t.me/…, id:123 (личка/бот). Без чата — текущий.
# Срок/дата начала: 12h, 90d, 2w, 6mo, 1y, 29.09, 29.09.2026, 2026-09-29, вчера.
# Конец: до:<дата> / to:<дата> (включительно).
# Размер: 50 = 50 МБ, также 500mb, 1.5gb. Типы медиа: photo, video, round,
# voice, audio, gif, sticker, file (и по-русски: фото, видео, кружки, голосовые,
# музыка, гиф, стикеры, файлы).
#
# Архив приходит файлом в чат, где введена команда (лучше «Избранное»),
# и остаётся на сервере в папке dumps/ рядом с модулями TGPy. Если он больше
# лимита Telegram (2 ГБ, с Premium 4 ГБ) — режется на несколько zip
# (part01, part02, …), каждый открывается отдельно. part:<размер> — свой размер.

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
DUMP_PART_MB = 1950            # размер части без Premium (лимит Telegram 2000 МБ)
DUMP_PART_PREMIUM_MB = 3900    # размер части с Premium (лимит 4000 МБ)
DUMP_PROGRESS_EVERY = 3        # секунд между обновлениями статуса
DUMP_KEEP_FOLDER = False       # оставлять ли распакованную папку рядом с zip
DUMP_KEEP_ARCHIVE = True       # оставлять ли zip на сервере после отправки
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
_DUMP_MEDIA = ("media", "медиа", "files", "файлы")
_DUMP_KINDS = {
    "photo": "photo", "photos": "photo", "фото": "photo",
    "video": "video", "videos": "video", "видео": "video",
    "round": "round", "кружки": "round", "кружок": "round",
    "voice": "voice", "голосовые": "voice", "голосовое": "voice",
    "audio": "audio", "music": "audio", "музыка": "audio", "аудио": "audio",
    "gif": "gif", "гиф": "gif",
    "sticker": "sticker", "stickers": "sticker", "стикеры": "sticker",
    "file": "file", "files": "file", "doc": "file", "docs": "file", "файлы": "file",
}
_DUMP_SIZE_UNITS = {"kb": 2**10, "кб": 2**10, "mb": 2**20, "мб": 2**20,
                    "gb": 2**30, "гб": 2**30}


def _dump_bytes(s: str, unit: int):
    """'50' → 50*unit, '500mb', '1.5gb' → байты; None, если не размер."""
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([a-zа-я]*)", s.strip().lower())
    if not m or (m[2] and m[2] not in _DUMP_SIZE_UNITS):
        return None
    return int(float(m[1].replace(",", ".")) * _DUMP_SIZE_UNITS.get(m[2], unit))


def _dump_media_opts(spec: str):
    """'photo,video,50' → (max_bytes | None, {kinds} | None)."""
    max_bytes, kinds = None, set()
    for x in filter(None, spec.lower().split(",")):
        if x in _DUMP_KINDS:
            kinds.add(_DUMP_KINDS[x])
        elif (b := _dump_bytes(x, 2**20)) is not None:
            max_bytes = b
        else:
            raise ValueError(f"не понял фильтр медиа: {x}")
    return max_bytes, kinds or None


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
    chat, since, until, limit = None, None, None, None
    opts = {"media": False, "media_max": None, "kinds": None, "part": None}
    for p in str(args).split():
        low = p.lower()
        pre_u = next((x for x in _DUMP_UNTIL if low.startswith(x)), None)
        pre_s = next((x for x in _DUMP_SINCE if low.startswith(x)), None)
        head, _, tail = low.partition(":")
        if head in _DUMP_MEDIA:
            opts["media"] = True
            if tail:
                opts["media_max"], opts["kinds"] = _dump_media_opts(tail)
        elif head in ("part", "parts", "часть", "части") and tail:
            opts["part"] = _dump_bytes(tail, 2**30)
            if not opts["part"] or opts["part"] < 10 * 2**20:
                raise ValueError(f"не понял размер части: {p} (минимум 10mb)")
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
    return chat, since, until, limit, opts


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


def _dump_kind(m) -> str:
    if m.photo:
        return "photo"
    if m.sticker:
        return "sticker"
    if m.voice:
        return "voice"
    if m.video_note:
        return "round"
    if m.gif:
        return "gif"
    if m.video:
        return "video"
    if m.audio:
        return "audio"
    if m.document:
        return "file"
    return ""  # опросы, гео, контакты — скачивать нечего


def _dump_pack(folder: Path, base: Path, part_bytes: int) -> list:
    """Раскладывает файлы папки по zip-частям не больше part_bytes.
    Оценка — по исходному размеру (сжатый не больше). Файл крупнее части
    режется на куски name.001, name.002… (склеить: cat name.* > name)."""
    slack = 2**20  # запас на заголовки zip
    files = sorted((p for p in folder.rglob("*") if p.is_file()),
                   key=lambda p: (p.parent.name == "media", str(p)))
    # части: список [(arcname, path, offset, length)]
    parts, cur, cur_size = [], [], 0
    for p in files:
        size, arc = p.stat().st_size, str(p.relative_to(folder))
        cost = size + 1024 + 2 * len(arc)
        if cost + slack <= part_bytes:
            if cur and cur_size + cost + slack > part_bytes:
                parts.append(cur)
                cur, cur_size = [], 0
            cur.append((arc, p, 0, size))
            cur_size += cost
            continue
        chunk = part_bytes - slack - 4096
        for i, off in enumerate(range(0, size, chunk), 1):
            if cur:
                parts.append(cur)
            cur, cur_size = [(f"{arc}.{i:03d}", p, off, min(chunk, size - off))], part_bytes
    if cur:
        parts.append(cur)

    paths = []
    for n, entries in enumerate(parts, 1):
        zp = (base.with_suffix(".zip") if len(parts) == 1
              else base.with_name(f"{base.name}.part{n:02d}.zip"))
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for arc, p, off, length in entries:
                # медиа уже сжаты — не тратим время на повторное сжатие
                ct = zipfile.ZIP_STORED if p.parent.name == "media" else zipfile.ZIP_DEFLATED
                if off == 0 and length == p.stat().st_size:
                    z.write(p, arc, ct)
                    continue
                zi = zipfile.ZipInfo(arc, time.localtime(p.stat().st_mtime)[:6])
                zi.compress_type = ct
                with open(p, "rb") as src, z.open(zi, "w", force_zip64=True) as dst:
                    src.seek(off)
                    left = length
                    while left:
                        buf = src.read(min(left, 8 * 2**20))
                        if not buf:
                            break
                        dst.write(buf)
                        left -= len(buf)
        paths.append(zp)
    return paths


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
    chat_ref, since, until, limit, opts = _dump_parse(args)
    media, kinds = opts["media"], opts["kinds"]
    premium = bool(getattr(await client.get_me(), "premium", False))
    tg_max = (DUMP_PART_PREMIUM_MB if premium else DUMP_PART_MB) * 2**20
    part_bytes = min(opts["part"] or tg_max, tg_max)
    part_note = ""
    if opts["part"] and opts["part"] > tg_max * 1.1:  # part:2 / part:4 — молча под лимит
        part_note = (f"Часть уменьшена до {tg_max // 2**20} МБ — лимит Telegram"
                     + ("" if premium else " без Premium"))
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

    count = media_count = media_skipped = media_filtered = 0
    senders: dict = {}
    last_edit = 0.0
    max_bytes = opts["media_max"] or DUMP_MEDIA_MAX_MB * 2**20 or 0
    # файл больше части Telegram не пропустит всё равно
    max_bytes = min(max_bytes, part_bytes - 2 * 2**20) if max_bytes else part_bytes - 2 * 2**20

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
                kind = _dump_kind(m)
                if media and kind and kinds and kind not in kinds:
                    media_filtered += 1
                elif media and kind:
                    if _dump_size(m) > max_bytes:
                        media_skipped += 1
                        label += f" (не скачано: {_dump_size(m) / 2**20:.1f} МБ)"
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

    try:
        await msg.edit(f"📦 Дамп «{title}»: упаковываю {count} сообщений…")
    except Exception:
        pass
    zips = await asyncio.to_thread(_dump_pack, folder, folder, part_bytes)
    if not DUMP_KEEP_FOLDER:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)

    total_mb = sum(z.stat().st_size for z in zips) / 2**20
    info = [f"✅ «{title}»: сообщений {count}"]
    if media:
        line = f"Медиа: {media_count}"
        if media_skipped:
            line += f", крупнее {max_bytes / 2**20:.0f} МБ не скачано: {media_skipped}"
        if media_filtered:
            line += f", других типов: {media_filtered}"
        info.append(line)
    info.append(f"Архив: {total_mb:.1f} МБ"
                + (f", частей: {len(zips)} по ≤{part_bytes / 2**20:.0f} МБ"
                   if len(zips) > 1 else ""))
    if part_note:
        info.append(part_note)

    sent = 0
    for n, zp in enumerate(zips, 1):
        tag = f"Часть {n}/{len(zips)}" if len(zips) > 1 else "Архив"
        t0 = 0.0

        async def up(done, total, tag=tag):
            nonlocal t0
            if time.monotonic() - t0 < DUMP_PROGRESS_EVERY:
                return
            t0 = time.monotonic()
            try:
                await msg.edit(f"📤 Отправляю {tag.lower()}: {done * 100 // max(total, 1)}%")
            except Exception:
                pass

        try:
            await client.send_file(
                msg.chat_id, str(zp), force_document=True, progress_callback=up,
                caption="\n".join(info) if n == 1 else tag)
            sent += 1
            if not DUMP_KEEP_ARCHIVE:
                zp.unlink(missing_ok=True)
        except Exception as e:
            info.append(f"❌ {tag} не отправлена ({e.__class__.__name__}), лежит на сервере:")
            info += [str(z) for z in zips[n - 1:]]
            return "\n".join(info)
    return "\n".join(info) + f"\n(отправлено файлов: {sent})"


def _dump_transformer(code: str) -> str:
    m = re.fullmatch(r"\s*[./]dump(?:\s+(.*))?", code, re.S | re.I)
    if not m:
        return code
    return f"await dump({(m[1] or '').strip()!r})"


tgpy.api.code_transformers.add("dump", _dump_transformer)
