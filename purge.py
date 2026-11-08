"""
    name: purge
    origin: https://github.com/com-master/tgpy-scripts/purge.py
    priority: 1002
    description: Удаление всех сообщений в группе по id (или старше даты), включая все топики
"""

# usage:
#     .purge -1001234567890           — предпросмотр: что и сколько будет удалено
#     .purge -1001234567890 confirm   — удалить ВСЕ сообщения (нужны права админа
#                                       на удаление), все топики форума — целиком
#     .purge -1001234567890 mine      — предпросмотр: только мои сообщения
#     .purge -1001234567890 mine confirm — удалить только свои (права не нужны)
#
# Только сообщения старше даты (до неё, сама дата не входит):
#     .purge -1001234567890 90d               — предпросмотр: старше 90 дней
#     .purge -1001234567890 до:29.09.2026     — старше 29.09.2026 00:00
#     .purge -1001234567890 mine 2w confirm   — удалить свои старше 2 недель
# Относительно: 12h/ч, 90d/д, 2w/н, 6mo/мес, 1y/г. Даты: 29.09, 29.09.2026,
# 2026-09-29, 2026-09-29T14:30, сегодня, вчера. Префикс before:/до: не обязателен.
#
# В режиме mine удаляются и сообщения, отправленные от имени твоих каналов
# (Premium «Отправлять как…»). Каналы берутся из списка «Отправлять как» этой
# группы; если какого-то там уже нет — добавь вручную: as:@channel / as:-100…
#     .purge -1001234567890 mine as:@mychannel confirm
#
# Чат можно указать как -100…, @username, ссылку t.me/… или id:123.
# Без `confirm` ничего не удаляется. Удаление необратимо.
# Команду лучше вводить в другом чате (например, в «Избранном»).

import asyncio
import re
import time
from datetime import datetime, timedelta

import tgpy.api
from telethon import errors, types, utils
from telethon.tl.functions.channels import GetSendAsRequest

try:  # Telethon >= 1.44
    from telethon.tl.functions.messages import (
        DeleteTopicHistoryRequest,
        GetForumTopicsRequest,
    )

    def _topics_req(chat, **kw):
        return GetForumTopicsRequest(peer=chat, **kw)

    def _del_topic_req(chat, top_id):
        return DeleteTopicHistoryRequest(peer=chat, top_msg_id=top_id)
except ImportError:  # старые версии Telethon
    from telethon.tl.functions.channels import (
        DeleteTopicHistoryRequest,
        GetForumTopicsRequest,
    )

    def _topics_req(chat, **kw):
        return GetForumTopicsRequest(channel=chat, **kw)

    def _del_topic_req(chat, top_id):
        return DeleteTopicHistoryRequest(channel=chat, top_msg_id=top_id)

# ─── Настройки ────────────────────────────────────────────────────────────────
PURGE_BATCH = 100              # сообщений за один запрос (максимум Telegram)
PURGE_PROGRESS_EVERY = 3       # секунд между обновлениями статуса
GENERAL_TOPIC_ID = 1           # «Общий» топик нельзя удалить, только очистить
# ──────────────────────────────────────────────────────────────────────────────

_CONFIRM = {"confirm", "да", "yes"}
_MINE = {"mine", "my", "мои", "свои"}
_BEFORE_PREFIXES = ("before:", "older:", "до:", "старше:")
_LOCAL_TZ = datetime.now().astimezone().tzinfo
_UNITS = {  # единица → timedelta на 1
    "h": timedelta(hours=1), "ч": timedelta(hours=1),
    "d": timedelta(days=1), "д": timedelta(days=1),
    "w": timedelta(weeks=1), "н": timedelta(weeks=1),
    "mo": timedelta(days=30), "мес": timedelta(days=30),
    "y": timedelta(days=365), "г": timedelta(days=365),
}


async def _call(coro_fn, *args, **kwargs):
    """Вызов с повтором при FloodWait."""
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)


def _ref(v: str):
    return int(v) if re.fullmatch(r"-?\d+", v) else v


def _parse_cutoff(s: str):
    """90d / 2w / 29.09.2026 / 2026-09-29T14:30 → datetime (aware) или None."""
    s = s.strip().lower()
    now = datetime.now(_LOCAL_TZ).replace(second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.fullmatch(r"(\d+)\s*([a-zа-я]+)", s)
    if m and m[2] in _UNITS:
        return now - int(m[1]) * _UNITS[m[2]]
    if s in ("today", "сегодня"):
        return today
    if s in ("yesterday", "вчера"):
        return today - timedelta(days=1)
    for fmt in ("%Y-%m-%dT%H:%M", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_LOCAL_TZ)
        except ValueError:
            pass
    try:
        return datetime.strptime(s, "%d.%m").replace(year=today.year, tzinfo=_LOCAL_TZ)
    except ValueError:
        return None


def _fmt(d: datetime) -> str:
    return d.astimezone(_LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def _parse(args: str):
    chat_ref, mine, confirm, extra, cutoff = None, False, False, [], None
    for p in str(args).split():
        low = p.lower()
        pref = next((x for x in _BEFORE_PREFIXES if low.startswith(x)), None)
        if pref or (not low.startswith("-") and _parse_cutoff(low) is not None):
            if cutoff is not None:
                raise ValueError("дата указана дважды")
            cutoff = _parse_cutoff(p[len(pref):] if pref else p)
            if cutoff is None:
                raise ValueError(f"не понял дату: {p}")
            if cutoff > datetime.now(_LOCAL_TZ):
                raise ValueError(f"дата в будущем: {p}")
        elif low.startswith("as:"):
            extra.append(_ref(p[3:]))
            mine = True
        elif low in _CONFIRM:
            confirm = True
        elif low in _MINE:
            mine = True
        elif chat_ref is None:
            chat_ref = _ref(p.split(":", 1)[1] if low.startswith(("id:", "chat:")) else p)
        else:
            raise ValueError(f"лишний аргумент: {p}")
    if chat_ref is None:
        raise ValueError("укажи id чата: .purge -1001234567890")
    return chat_ref, mine, confirm, extra, cutoff


async def _my_senders(chat, extra) -> list:
    """Я + мои каналы, от имени которых можно писать в этот чат (Premium)."""
    senders = [await client.get_me()]
    try:
        res = await _call(client, GetSendAsRequest(peer=chat))
        ents = {utils.get_peer_id(e): e for e in [*res.chats, *res.users]}
        for sp in res.peers:
            e = ents.get(utils.get_peer_id(sp.peer))
            if e is not None:
                senders.append(e)
    except errors.RPCError:
        pass  # обычная группа / «отправлять как» недоступно
    for ref in extra:
        senders.append(await client.get_entity(ref))
    uniq = {}
    for e in senders:
        uniq.setdefault(utils.get_peer_id(e), e)
    return list(uniq.values())


def _name(e) -> str:
    return getattr(e, "title", None) or getattr(e, "first_name", None) or str(e.id)


async def _count(chat, senders, cutoff=None) -> int:
    # С датой — через поиск (search=""), он учитывает max_date в счётчике.
    kw = {"offset_date": cutoff, "search": ""} if cutoff else {}
    if senders is None:
        return (await client.get_messages(chat, limit=0, **kw)).total
    total = 0
    for s in senders:
        total += (await client.get_messages(chat, limit=0, from_user=s, **kw)).total
    return total


async def _get_topics(chat) -> list:
    """Все топики форума (без удалённых)."""
    topics, offset_date, offset_id, offset_topic = [], None, 0, 0
    while True:
        res = await _call(
            client,
            _topics_req(
                chat,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
            ),
        )
        page = [t for t in res.topics if isinstance(t, types.ForumTopic)]
        new = [t for t in page if t.id not in {x.id for x in topics}]
        topics.extend(new)
        if not res.topics or not new or len(topics) >= res.count:
            return topics
        last = res.topics[-1]
        offset_date = getattr(last, "date", None)
        offset_id = getattr(last, "top_message", 0)
        offset_topic = last.id


class _Progress:
    def __init__(self, msg, title):
        self.msg, self.title, self.last = msg, title, 0.0

    async def __call__(self, text, force=False):
        if not force and time.monotonic() - self.last < PURGE_PROGRESS_EVERY:
            return
        self.last = time.monotonic()
        try:
            await self.msg.edit(f"🗑 {self.title}\n{text}")
        except Exception:
            pass


async def _delete_ids(chat, ids) -> int:
    """Удаляет пачку; если пачка целиком не прошла — по одному."""
    try:
        res = await _call(client.delete_messages, chat, ids, revoke=True)
        return sum(r.pts_count for r in res) if res else len(ids)
    except errors.RPCError:
        done = 0
        for i in ids:
            try:
                await _call(client.delete_messages, chat, [i], revoke=True)
                done += 1
            except errors.RPCError:
                pass  # служебные/неудаляемые сообщения
        return done


async def _sweep(chat, progress, skip_id, done_topics, senders, cutoff=None) -> int:
    deleted = 0
    for sender in senders or [None]:  # None — все сообщения
        batch = []
        kw = {"from_user": sender} if sender is not None else {}
        if cutoff:
            kw["offset_date"] = cutoff  # только старше даты
        async for m in client.iter_messages(chat, **kw):
            if m.id == skip_id or (cutoff and m.date >= cutoff):
                continue
            batch.append(m.id)
            if len(batch) >= PURGE_BATCH:
                deleted += await _delete_ids(chat, batch)
                batch = []
                await progress(f"Топиков удалено: {done_topics}\nСообщений удалено: {deleted}")
        if batch:
            deleted += await _delete_ids(chat, batch)
    return deleted


async def purge(args: str = ""):
    msg = ctx.msg
    ctx.is_manual_output = True
    try:
        status = await _purge(msg, args)
    except ValueError as e:
        status = f"❌ {e}"
    except Exception as e:
        status = f"❌ Ошибка: {e!r}"
    try:
        await msg.edit(status)
    except Exception:  # сообщение могло быть удалено вместе с чатом
        pass
    return status


async def _purge(msg, args):
    chat_ref, mine, confirm, extra, cutoff = _parse(args)
    try:
        chat = await client.get_entity(chat_ref)
    except Exception as e:
        return f"❌ Не нашёл чат {chat_ref}: {e!r}"
    if isinstance(chat, types.User):
        return "❌ Это не группа. Скрипт работает только с группами и каналами."

    title = getattr(chat, "title", str(chat_ref))
    is_forum = bool(getattr(chat, "forum", False))
    perms = await client.get_permissions(chat, "me")
    can_delete_all = bool(perms.is_creator or perms.delete_messages)

    if not mine and not can_delete_all:
        return (
            f"❌ В «{title}» нет прав на удаление чужих сообщений.\n"
            f"Удалить только свои: .purge {chat_ref} mine confirm"
        )

    skip_id = msg.id if msg.chat_id == utils.get_peer_id(chat) else None  # саму команду
    senders = await _my_senders(chat, extra) if mine else None
    total = await _count(chat, senders, cutoff)
    topics = await _get_topics(chat) if is_forum else []
    whole_topics = is_forum and not mine and cutoff is None

    if not confirm:
        lines = [
            f"⚠️ Предпросмотр «{title}» (id {chat.id})",
            f"Режим: {'только мои сообщения' if mine else 'ВСЕ сообщения'}",
        ]
        if senders:
            lines.append("От имени: " + ", ".join(_name(e) for e in senders))
        if cutoff:
            lines.append(f"Старше: {_fmt(cutoff)} (более новые не трогаются)")
        lines.append(f"Сообщений: ~{total}")
        if is_forum:
            lines.append(f"Топиков: {len(topics)}"
                         + (" (будут удалены целиком, кроме «Общего»)" if whole_topics
                            else " (сообщения удаляются по одному, топики остаются)"))
        # Дата в команде — абсолютная, чтобы удалилось ровно то, что в предпросмотре.
        cmd = " ".join([".purge", str(chat_ref)] + (["mine"] if mine else [])
                       + [f"as:{r}" for r in extra]
                       + ([f"before:{cutoff.astimezone(_LOCAL_TZ):%Y-%m-%dT%H:%M}"]
                          if cutoff else [])
                       + ["confirm"])
        lines += ["", "Удаление необратимо. Чтобы удалить, отправь:", cmd]
        return "\n".join(lines)

    progress = _Progress(msg, f"Удаляю в «{title}»…")
    await progress("начинаю", force=True)

    done_topics = 0
    if whole_topics:
        for t in topics:
            if t.id == GENERAL_TOPIC_ID:
                continue
            while True:  # API удаляет историю топика порциями
                res = await _call(client, _del_topic_req(chat, t.id))
                if not getattr(res, "offset", 0):
                    break
            done_topics += 1
            await progress(f"Топиков удалено: {done_topics}/{len(topics)}")

    deleted = await _sweep(chat, progress, skip_id, done_topics, senders, cutoff)

    left = await _count(chat, senders, cutoff)
    result = [f"✅ «{title}»: удалено сообщений: {deleted}"]
    if cutoff:
        result[0] += f" (старше {_fmt(cutoff)})"
    if done_topics:
        result.append(f"Топиков удалено целиком: {done_topics}")
    if left:
        result.append(f"Осталось (служебные/неудаляемые): {left}")
    return "\n".join(result)


def _purge_transformer(code: str) -> str:
    m = re.fullmatch(r"\s*[./]purge(?:\s+(.*))?", code, re.S | re.I)
    if not m:
        return code
    return f"await purge({(m[1] or '').strip()!r})"


tgpy.api.code_transformers.add("purge", _purge_transformer)
