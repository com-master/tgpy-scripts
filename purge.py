"""
    name: purge
    origin: https://github.com/com-master/tgpy-scripts/purge.py
    priority: 1002
    description: Удаление всех сообщений в группе по id, включая все топики
"""

# usage:
#     .purge -1001234567890           — предпросмотр: что и сколько будет удалено
#     .purge -1001234567890 confirm   — удалить ВСЕ сообщения (нужны права админа
#                                       на удаление), все топики форума — целиком
#     .purge -1001234567890 mine      — предпросмотр: только мои сообщения
#     .purge -1001234567890 mine confirm — удалить только свои (права не нужны)
#
# Чат можно указать как -100…, @username, ссылку t.me/… или id:123.
# Без `confirm` ничего не удаляется. Удаление необратимо.
# Команду лучше вводить в другом чате (например, в «Избранном»).

import asyncio
import re
import time

import tgpy.api
from telethon import errors, types, utils

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


async def _call(coro_fn, *args, **kwargs):
    """Вызов с повтором при FloodWait."""
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)


def _parse(args: str):
    chat_ref, mine, confirm = None, False, False
    for p in str(args).split():
        low = p.lower()
        if low in _CONFIRM:
            confirm = True
        elif low in _MINE:
            mine = True
        elif chat_ref is None:
            if re.fullmatch(r"-?\d+", p):
                chat_ref = int(p)
            elif low.startswith(("id:", "chat:")):
                v = p.split(":", 1)[1]
                chat_ref = int(v) if re.fullmatch(r"-?\d+", v) else v
            else:
                chat_ref = p
        else:
            raise ValueError(f"лишний аргумент: {p}")
    if chat_ref is None:
        raise ValueError("укажи id чата: .purge -1001234567890")
    return chat_ref, mine, confirm


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


async def _sweep(chat, progress, skip_id, done_topics, from_me) -> int:
    deleted, batch = 0, []
    kw = {"from_user": "me"} if from_me else {}
    async for m in client.iter_messages(chat, **kw):
        if m.id == skip_id:
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
    chat_ref, mine, confirm = _parse(args)
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
    total = (await client.get_messages(chat, limit=0, **({"from_user": "me"} if mine else {}))).total
    topics = await _get_topics(chat) if is_forum else []

    if not confirm:
        lines = [
            f"⚠️ Предпросмотр «{title}» (id {chat.id})",
            f"Режим: {'только мои сообщения' if mine else 'ВСЕ сообщения'}",
            f"Сообщений: ~{total}",
        ]
        if is_forum:
            lines.append(f"Топиков: {len(topics)}"
                         + ("" if mine else " (будут удалены целиком, кроме «Общего»)"))
        cmd = f".purge {chat_ref}{' mine' if mine else ''} confirm"
        lines += ["", "Удаление необратимо. Чтобы удалить, отправь:", cmd]
        return "\n".join(lines)

    progress = _Progress(msg, f"Удаляю в «{title}»…")
    await progress("начинаю", force=True)

    done_topics = 0
    if is_forum and not mine:
        for t in topics:
            if t.id == GENERAL_TOPIC_ID:
                continue
            while True:  # API удаляет историю топика порциями
                res = await _call(client, _del_topic_req(chat, t.id))
                if not getattr(res, "offset", 0):
                    break
            done_topics += 1
            await progress(f"Топиков удалено: {done_topics}/{len(topics)}")

    deleted = await _sweep(chat, progress, skip_id, done_topics, from_me=mine)

    left = (await client.get_messages(chat, limit=0, **({"from_user": "me"} if mine else {}))).total
    result = [f"✅ «{title}»: удалено сообщений: {deleted}"]
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
