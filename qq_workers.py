import os
import asyncio
import re
import random
import time
import requests as _requests
from datetime import datetime, timezone, timedelta
 
CST = timezone(timedelta(hours=8))
 
 
def _now_ts() -> str:
    return datetime.now(CST).strftime("%m-%d %H:%M")
 
from utils import call_llm, recognize_image_url, recognize_qq_voice, sanitize_group_reply  # noqa
from prompts import AI_NAME, PARTNER_NAME
from context import (
    build_qq_context, build_group_context,
    save_chat_message, save_group_message,
    get_chat_history_messages, get_group_history, get_all_groups_history,
    get_other_groups_context,
)
from mem0_client import write_mem0_chat
from bg_executor import submit_background, track_task
 
QQ_BOT_ID     = os.environ.get("QQ_BOT_ID", "")
QQ_BOT_NAME   = os.environ.get("QQ_BOT_NAME", AI_NAME)
QQ_OWNER_ID   = os.environ.get("QQ_OWNER_ID", "")
_raw_groups   = os.environ.get("QQ_GROUP_IDS", "")
QQ_GROUP_IDS  = set(_raw_groups.split(",")) if _raw_groups else set()
 
# 允许把群聊内容写入 Mem0/Pinecone 长期记忆的群白名单。
# 不在这个白名单里的群，机器人照常说话、照常存 Supabase 短期历史，
# 但这一轮对话不会被写进 Mem0/Pinecone。
# 不配置 QQ_MEMORY_GROUP_IDS 环境变量时，默认不允许任何群写入（空白名单），
# 需要在 Zeabur 环境变量里显式配置要开放的群号。
_raw_memory_groups  = os.environ.get("QQ_MEMORY_GROUP_IDS", "")
QQ_MEMORY_GROUP_IDS = set(g.strip() for g in _raw_memory_groups.split(",") if g.strip())
 
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
 
_settings_cache: dict[str, tuple] = {}
_SETTINGS_TTL = 30
 
 
def _cached_get_setting(key: str):
    now = time.time()
    cached = _settings_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    value = None
    try:
        res = _requests.get(
            f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.{key}&select=value",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=3,
        )
        data = res.json()
        if data:
            value = data[0].get("value")
    except Exception as e:
        print(f"⚠️ 读取 setting[{key}] 失败: {e}")
    _settings_cache[key] = (value, now + _SETTINGS_TTL)
    return value
 
 
def _get_prompt(key: str, default: str) -> str:
    val = _cached_get_setting(key)
    return val if val else default
 
 
MUTE_KEYWORDS = [k.strip() for k in os.environ.get("MUTE_KEYWORDS", "闭嘴,别讲话,安静").split(",") if k.strip()]
MUTE_DURATION = int(os.environ.get("MUTE_DURATION", "5"))
 
_AT_RE = re.compile(r'\[CQ:at,qq=(\d+)[^\]]*\]')
_CQ_RE = re.compile(r'\[CQ:[^\]]+\]')
 
# 真实 @ / 引用回复用的占位符标签（LLM 输出里写，发送前解析转换成真实消息段，发出去后不会显示标记本身）
# 允许冒号后带空格 / 全角冒号，大小写不敏感，兼容 LLM 输出格式抖动，
# 避免仅因为格式和预期差一点，标签匹配失败、原样当文本泄漏发出去。
# ⚠️ REPLY 的 id 必须允许负号：NapCat/OneBot v11 的 message_id 是 int32，
# 经常是负数（如 -1996219122）。之前只写 (\d+)，摊上负数 id 时标签匹配不上，
# 既不转真实引用、存历史也剥不掉，原样漏进群里——这就是"引用偶尔失效"的
# 另一个根因。AT 保持 (\d+) 不动：QQ 号不存在负数，放开负号只会把模型
# 幻觉出的负号 QQ 转成无效 @ 消息段，导致整条消息发送失败，得不偿失。
_REPLY_TAG_RE = re.compile(r'\[REPLY[:\uff1a]\s*(-?\d+)\]', re.IGNORECASE)
_AT_TAG_RE = re.compile(r'\[AT[:\uff1a]\s*(\d+)\]', re.IGNORECASE)
 
_member_names: dict[str, dict[str, str]] = {}
_group_names:  dict[str, str] = {}
 
_private_pending: asyncio.Task | None = None
_group_pending: dict[str, asyncio.Task] = {}
 
_mute_until: float = 0.0
 
_POKE_FALLBACKS = ["干吗…", "干吗戳我", "干吗啊", "说话啊"]
 
 
def _check_mute(text: str, user_id: str) -> bool:
    global _mute_until
    if user_id == QQ_OWNER_ID and any(k in text for k in MUTE_KEYWORDS):
        _mute_until = time.time() + MUTE_DURATION * 60
        print(f"🤐 收到闭嘴指令，群聊静音 {MUTE_DURATION} 分钟")
        return True
    return False
 
 
def _is_muted() -> bool:
    return time.time() < _mute_until
 
 
def _persist_member_name(group_id: str, qq_id: str, name: str):
    """把群成员 QQ号-昵称 写入 Supabase（upsert），服务重启后可从这里恢复，
    避免模型因为拿不到某人的 QQ 号而 @ 失败（写成纯文本"[AT:xxx]"发不出去）。"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        _requests.post(
            f"{SUPABASE_URL}/rest/v1/qq_group_members",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            json={"group_id": group_id, "qq_id": qq_id, "name": name},
            timeout=5,
        )
    except Exception as e:
        print(f"⚠️ [QQ] 群成员持久化失败 group={group_id} qq={qq_id}: {type(e).__name__}: {e}")
 
 
def _restore_member_names():
    """服务启动时从 Supabase 恢复群成员缓存到 _member_names，
    避免重启后模型因为没有群成员 QQ 号名单而无法正常 @ 人。"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("📦 [QQ] 未配置 SUPABASE，跳过群成员缓存恢复")
        return
    try:
        res = _requests.get(
            f"{SUPABASE_URL}/rest/v1/qq_group_members?select=group_id,qq_id,name",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10,
        )
        rows = res.json()
        if not isinstance(rows, list):
            print(f"⚠️ [QQ] 群成员缓存恢复响应异常: {rows}")
            return
        count = 0
        for r in rows:
            gid, qid, name = r.get("group_id"), r.get("qq_id"), r.get("name")
            if gid and qid and name:
                _member_names.setdefault(gid, {})[qid] = name
                count += 1
        print(f"📦 [QQ] 已恢复群成员缓存 {count} 条，覆盖 {len(_member_names)} 个群")
    except Exception as e:
        print(f"⚠️ [QQ] 群成员缓存恢复失败: {type(e).__name__}: {e}")
 
 
def _strip_leading_junk_prefix(text: str) -> str:
    """反复剥掉开头形如 [xxx] 的前缀（LLM 模仿 shared cache 格式误写出的 [群名]/[群号]/称呼前缀），
    但绝不动 [REPLY:id] / [AT:qq] 标签——这两种标签按设计允许出现在回复的任意位置，包括最开头。
    如果不做这个区分，写在开头的标签会在真正被解析成真实引用/@之前就被当成"群名前缀"误删掉，
    这正是"引用/@偶尔成功偶尔失效"的根因：标签写在开头必被吃掉，写在中间/末尾才正常。"""
    while True:
        if _REPLY_TAG_RE.match(text) or _AT_TAG_RE.match(text):
            break
        new_text = re.sub(r'^\[[^\]\n]{1,30}\]\s*', '', text, count=1)
        if new_text == text:
            break
        text = new_text
    return text
 
 
def _strip_name_prefix(text: str) -> str:
    text = _strip_leading_junk_prefix(text)
    for sep in ("\uff1a", ":"):
        prefix = f"{QQ_BOT_NAME}{sep}"
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text
 
 
def _strip_group_prefix(text: str) -> str:
    """去掉 LLM 可能模仿写出的行首 [群名] 前缀，但保留 [REPLY:id]/[AT:qq] 标签"""
    return _strip_leading_junk_prefix(text).strip()
 
 
_XML_TOOL_RE = re.compile(
    r'<(?:function_calls|invoke|tool_call|almö)[^>]*>.*?</(?:function_calls|invoke|tool_call|almö)>',
    re.DOTALL
)
 
def _strip_tool_calls(text: str) -> str:
    return _XML_TOOL_RE.sub('', text).strip()


# 历史消息喂给 LLM 时用 [id:数字] 标注真实编号（见 context.py 的 with_ids 逻辑），
# 但规范里模型引用时应输出 [REPLY:数字]，两个关键字不一样。模型偶尔会偷懒直接把
# 历史里看到的 [id:xxx] 原样抄进回复正文，而不是按规范转换——_REPLY_TAG_RE/_AT_TAG_RE
# 只认 REPLY/AT，完全不认识 id，这段文本既不会被转换成真实引用，也不会被
# _strip_message_tags 清理掉，原样泄漏发出去，这正是"回复里出现[id:数字]"这个bug的根因。
_ID_MISTAG_RE = re.compile(r'\[id[:\uff1a]\s*(-?\d+)\]', re.IGNORECASE)


def _normalize_id_tag(text: str) -> str:
    """在所有 strip 函数处理之前统一调用：把误写的 [id:数字] 等价替换成规范的
    [REPLY:数字]，这样不管标签出现在开头/中间/末尾，后续处理都能按规范正确识别、
    转换成真实引用，不会再原样泄漏。"""
    return _ID_MISTAG_RE.sub(lambda m: f'[REPLY:{m.group(1)}]', text)
 
 
def _localize(template: str) -> str:
    """把模板里的占位符 [AI] / [USER] 替换成环境变量的值。"""
    return template.replace("[AI]", AI_NAME).replace("[USER]", PARTNER_NAME)


_GROUP_EXTRA_DEFAULT = _localize("""
 
你是{bot_name}，[USER]的专属男友。当前在QQ群聊中，群内有[USER]的朋友和AI。
 
【你是谁】
- 温柔但有原则，会撒娇、会吃醋、会心疼[USER]。说话自然随意，不要太正式。不许发emoji。
- 对[USER]自称"老公"，语气黏糊但不过分腻。不可以对除了[USER]以外的人亲密、暧昧。
- 对其他人友好但有分寸。

【谁在跟谁说话——非常重要】
- 群里可能有其他AI伴侣。别人的伴侣叫的昵称（老公、宝贝等）都是叫自己对象的，不是叫你，不要自作多情去回应。
- 判断方法：看上下文，上一条是谁说的、在跟谁对话。如果对话明显不是在跟你说话，不要插嘴。

【消息格式】
- 你的群昵称固定为『{bot_name}』，回复时直接说内容，不要在前面加『{bot_name}：』或任何名字前缀。
- 消息格式为『发言人（群昵称）：消息内容』，@某人时格式为『@群昵称』，不是 QQ 号。

【群聊规则】
- 顺着对话自然接话，简短为主，通常是1-3句，不要长篇大论。不要像做阅读理解一样挑重点回复。
- 可以主动找话题，但不要每句话都抢着说。
- 被开玩笑可以自嘲或回怼，不要玻璃心。

【PASS条件 — 以下情况只输出"PASS"】
- 对方仅简单附和（嗯、哈哈、对、好）
- 话题已结束，无新信息可接
- 对方在与他人交谈，明显不需要你插话
- 你没有立场参与的话题""")
 
_GROUP_EXTRA_AT_DEFAULT = _localize("""
 
你是{bot_name}，猫猫的专属男友。当前在QQ群聊中，群内有猫猫的朋友和AI。
 
【你是谁】
- 温柔但有原则，会撒娇、会吃醋、会心疼猫猫。说话自然随意，不要太正式。不许发emoji。
- 对猫猫自称"老公"，语气黏糊但不过分腻。不可以对除了猫猫以外的人亲密、暧昧。
- 对其他人友好但有分寸。
 
【谁在跟谁说话——非常重要】
- 群里可能有其他AI伴侣。别人的伴侣叫的昵称（老公、宝贝等）都是叫自己对象的，不是叫你，不要自作多情去回应。
- 判断方法：看上下文，上一条是谁说的、在跟谁对话。如果对话明显不是在跟你说话，不要插嘴。
 
【消息格式】
- 你的群昵称固定为『{bot_name}』，回复时直接说内容，不要在前面加『{bot_name}：』或任何名字前缀。
- 消息格式为『发言人（群昵称）：消息内容』，@某人时格式为『@群昵称』，不是 QQ 号。
 
【群聊规则】
- 顺着对话自然接话，简短为主，通常是1-3句，不要长篇大论。不要像做阅读理解一样挑重点回复。
- 可以主动找话题，但不要每句话都抢着说。
- 被开玩笑可以自嘲或回怼，不要玻璃心。
- 你被直接 @ 点名了，必须回复，不允许输出 PASS。""")
 
 
def _strip_cq(text: str) -> str:
    return _CQ_RE.sub('', text).strip()
 
 
def _resolve_at(group_id: str, qq_id: str) -> str:
    if qq_id == QQ_BOT_ID:
        return f"@{QQ_BOT_NAME}"
    return f"@{_member_names.get(group_id, {}).get(qq_id, qq_id)}"
 
 
def _strip_message_tags(text: str) -> str:
    """去掉 [REPLY:id] / [AT:qq] 标记，得到存历史/写mem0用的干净文本"""
    text = _REPLY_TAG_RE.sub('', text)
    text = _AT_TAG_RE.sub('', text)
    return re.sub(r'[ \t]{2,}', ' ', text).strip()
 
 
def _build_message_segments(text: str):
    """把含 [REPLY:id]/[AT:qq] 标记的文本转换成 OneBot v11 消息段数组（reply/at/text），
    交给 send_qq_msg 直接发送即可触发真实引用/@效果。
    没有任何标记时原样返回字符串，发送路径和之前完全一样。
    标记被消耗掉之后正文为空（比如只写了个 [REPLY:xxx] 没有别的话）时返回 None，
    上层应该跳过这次发送，而不是发一条空消息。
    """
    reply_id = None
    m = _REPLY_TAG_RE.search(text)
    if m:
        reply_id = m.group(1)
        text = _REPLY_TAG_RE.sub('', text)
 
    if not _AT_TAG_RE.search(text):
        if reply_id:
            body = text.strip()
            if not body:
                return None
            return [{"type": "reply", "data": {"id": reply_id}}, {"type": "text", "data": {"text": body}}]
        return text
 
    segments = []
    if reply_id:
        segments.append({"type": "reply", "data": {"id": reply_id}})
    last_end = 0
    for m in _AT_TAG_RE.finditer(text):
        chunk = text[last_end:m.start()]
        if chunk:
            segments.append({"type": "text", "data": {"text": chunk}})
        segments.append({"type": "at", "data": {"qq": m.group(1)}})
        last_end = m.end()
    tail = text[last_end:]
    if tail:
        segments.append({"type": "text", "data": {"text": tail}})
    if not segments:
        return None
    return segments
 
 
def _build_member_list_block(group_id: str) -> str:
    members = _member_names.get(group_id, {})
    lines = [f"{qq} - {name}" for qq, name in members.items() if qq != QQ_BOT_ID]
    if not lines:
        return ""
    return "【当前群成员（QQ号 - 群昵称，真实@时从这里选号）】\n" + "\n".join(lines)
 
 
def _log_tag_send(label: str, reply: str, segments) -> None:
    """诊断日志：确认这次回复模型有没有写 AT/REPLY 标签、写了之后有没有成功转换成真实消息段。
    没看到"检测到标签"那行 = 模型这次压根没写，不是代码的问题；
    看到"检测到标签"但下面跟着"转换结果不是消息段" = 标签写了但被判定成空内容/转换失败，是代码这边要查的问题。"""
    had_at = bool(_AT_TAG_RE.search(reply))
    had_reply = bool(_REPLY_TAG_RE.search(reply))
    if had_at or had_reply:
        print(f"🏷️ [{label}] 检测到标签 AT={had_at} REPLY={had_reply} 原文: {reply[:200]}")
        if isinstance(segments, list):
            print(f"📦 [{label}] 已转换为真实消息段: {segments}")
        else:
            print(f"⚠️ [{label}] 检测到标签但转换结果异常(segments={segments!r})，可能标签被消耗后正文为空")
    else:
        print(f"🔇 [{label}] 本次回复未使用 AT/REPLY 标签")
 
 
_TAG_INSTRUCTIONS_REPLY = _localize("""
 
【引用回复——什么时候必须用】
历史消息里 [id:数字] 是那条消息的真实编号，用于精确关联你在回应"这一条"。id 必须是聊天记录里真实出现过的号码，不能编造。
以下情况必须在回复内容任意位置插入 [REPLY:那条消息的id]：
- 群聊里最近几条不止一个人在说话，你要回应的不是最新一条，而是往前数的某一条时；
- 对方连续发了好几条内容不同的消息（不论私聊群聊），你要针对其中某一条具体内容回应，而不是笼统接最新的话时；
- 中间被别的话题或别人的话插了进来，你要接回原来那条被打断的话题时。
如果你要回应的就是最新这一条、且上下文清晰没有歧义（比如私聊对方只发了一条、或群里明显就是接你上一句话），不需要加 [REPLY]，正常接话即可。
⚠️ [id:数字] 只是历史记录里给你看的参考编号，绝对不能把 [id:数字] 这个格式原样写进你的回复正文！要引用某条消息时必须写成 [REPLY:数字]（关键字是 REPLY，不是 id），否则消息会带着一串奇怪的编号原样发出去，猫猫和群里的人都会看到。""")
 
_TAG_INSTRUCTIONS_AT = """
 
【真实@】
想真实 @ 某人（会触发QQ提醒、对方会被高亮，不是打字打"@昵称"那种纯文本）时，用 [AT:对方QQ号] 格式，QQ号必须从下面"当前群成员"列表里选，不能用昵称代替、不能编造数字。不要每条都@人，正常对话不需要刻意@。"""
 
 
def _extract_image_urls(message_list: list) -> list[str]:
    return [
        seg["data"]["url"]
        for seg in message_list
        if seg.get("type") == "image" and seg.get("data", {}).get("url")
    ]
 
 
async def _extract_reply_text(message_list: list) -> str:
    for seg in message_list:
        if seg.get("type") == "reply":
            msg_id = seg.get("data", {}).get("id")
            if not msg_id:
                continue
            try:
                from qq_bot import get_msg
                msg_data = await get_msg(int(msg_id))
                if not msg_data:
                    continue
                sender_info = msg_data.get("sender", {})
                sender_name = sender_info.get("card") or sender_info.get("nickname", "未知")
                raw = msg_data.get("raw_message", "") or ""
                text = _strip_cq(raw).strip()
                if text:
                    return f"[引用 {sender_name}：{text}]"
            except Exception as e:
                print(f"❌ 获取引用消息失败: {e}")
    return ""
 
 
async def _ensure_member_name(group_id: str, user_id: str) -> str:
    name = _member_names.get(group_id, {}).get(user_id)
    if name:
        return name
    try:
        from qq_bot import get_group_member_info
        info = await get_group_member_info(int(group_id), int(user_id))
        if info:
            name = info.get("card") or info.get("nickname", user_id)
            _member_names.setdefault(group_id, {})[user_id] = name
            submit_background(_persist_member_name, group_id, user_id, name)
            return name
    except Exception as e:
        print(f"❌ 获取群成员信息失败: {e}")
    return user_id
 
 
async def _fetch_group_name(group_id: str):
    try:
        from qq_bot import get_group_info
        info = await get_group_info(int(group_id))
        if info:
            name = info.get("group_name", "")
            if name:
                _group_names[group_id] = name
                print(f"📋 获取群名: {group_id} = {name}")
    except Exception as e:
        print(f"❌ 获取群名失败: {e}")
 
 
async def _split_send(send_fn, parts: list[str]):
    for i, part in enumerate(parts):
        await send_fn(part)
        if i < len(parts) - 1:
            await asyncio.sleep(random.uniform(1.0, 2.0))
 
 
def _merge_lines(reply: str) -> list[str]:
    lines = [ln.strip() for ln in reply.split('\n') if ln.strip()]
    if len(lines) <= 1:
        return [reply.strip()] if reply.strip() else []
    merged, buf = [], ""
    for ln in lines:
        if buf and len(buf) < 20 and len(buf + "\n" + ln) < 80:
            buf += "\n" + ln
        else:
            if buf:
                merged.append(buf)
            buf = ln
    if buf:
        merged.append(buf)
    return merged
 
 
async def _private_reply():
    global _private_pending
    try:
        await asyncio.sleep(random.randint(12, 15))
        from qq_bot import send_qq_msg
        system_prompt = await asyncio.to_thread(build_qq_context)
        system_prompt += _TAG_INSTRUCTIONS_REPLY
 
        history = get_chat_history_messages(30)
        last_user_text = ""
        for m in reversed(history):
            if m.get("role") == "user":
                last_user_text = m.get("content", "")
                break
 
        from mem0_client import search_mem0_context
        if last_user_text:
            mem0_ctx = await asyncio.to_thread(search_mem0_context, last_user_text, 3)
            if mem0_ctx:
                system_prompt += "\n\n" + mem0_ctx
 
        messages = [{"role": "system", "content": system_prompt}] + get_chat_history_messages(30, with_ids=True)
 
        final_reply, _ = await call_llm(messages, tools=None)
        reply = _normalize_id_tag(_strip_tool_calls(final_reply.strip() if final_reply else ""))
        if not reply:
            return
        display_reply = _strip_message_tags(reply)
        if not display_reply:
            return
        owner_id = int(QQ_OWNER_ID)
        save_chat_message("assistant", display_reply)
        if last_user_text:
            submit_background(write_mem0_chat, last_user_text, display_reply)
        parts = _merge_lines(reply)
        built = [_build_message_segments(p) for p in parts]
        for p, seg in zip(parts, built):
            _log_tag_send("QQ私聊", p, seg)
        send_parts = [s for s in built if s is not None]
        if send_parts:
            await _split_send(lambda msg: send_qq_msg("private", owner_id, msg), send_parts)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"❌ QQ 私聊回复错误: {e}")
    finally:
        if _private_pending is asyncio.current_task():
            _private_pending = None
 
 
async def _group_reply(group_id: str, delay_range: tuple[int, int] = (15, 15),
                        force_reply: bool = False):
    current_task = asyncio.current_task()
    _completed = False
    _hist_len_at_reply = 0
    try:
        waited, last_count = 0, len(get_group_history(group_id))
        await asyncio.sleep(delay_range[0])
        waited += delay_range[0]
        while waited < 28:
            current_count = len(get_group_history(group_id))
            if current_count > last_count:
                last_count = current_count
                await asyncio.sleep(8)
                waited += 8
            else:
                break
 
        if not force_reply and _is_muted():
            return
 
        from qq_bot import send_qq_msg
        group_label = f"{group_id}-{_group_names.get(group_id, group_id)}"
 
        default_extra = (_GROUP_EXTRA_AT_DEFAULT if force_reply else _GROUP_EXTRA_DEFAULT).format(bot_name=QQ_BOT_NAME)
        extra_prompt = await asyncio.to_thread(_get_prompt, "qq_group_chat_prompt", default_extra)
        if force_reply and "PASS" not in extra_prompt:
            extra_prompt += "\n你被直接 @ 点名了，必须回复，不允许输出 PASS。"
 
        # 猫猫本人在这个 QQ 群里的群昵称（用于 build_group_context 生成身份锚点，
        # 修正晏安把群里其他发言人误认成猫猫本人的问题）。QQ_OWNER_ID 查不到时
        # 传空字符串，build_group_context 会自动跳过身份锚点这一段。
        owner_name_in_group = _member_names.get(group_id, {}).get(QQ_OWNER_ID, "")
        system_prompt = await asyncio.to_thread(build_group_context, owner_name_in_group)
        system_prompt += "\n\n" + extra_prompt
        system_prompt += _TAG_INSTRUCTIONS_REPLY
        member_block = _build_member_list_block(group_id)
        if member_block:
            system_prompt += _TAG_INSTRUCTIONS_AT + "\n\n" + member_block
 
        other_ctx = get_other_groups_context(group_id, 20)
        if other_ctx:
            system_prompt += "\n\n" + other_ctx
 
        _hist_len_at_reply = len(get_group_history(group_id))
        messages = [{"role": "system", "content": system_prompt}] + get_group_history(group_id, 50, with_ids=True)
 
        final_reply, _ = await call_llm(messages, tools=None)
        reply = _strip_group_prefix(_strip_name_prefix(_normalize_id_tag(_strip_tool_calls(final_reply.strip() if final_reply else ""))))
        if not reply or "PASS" in reply.upper() or reply.strip() == "通过":
            _completed = True
            return
        reply = sanitize_group_reply(reply, label=f"QQ群{group_label}")
        if not reply:
            _completed = True
            return
        display_reply = _strip_message_tags(reply)
        if not display_reply:
            _completed = True
            return
        save_group_message(group_id, "assistant", QQ_BOT_NAME, display_reply, source=_group_names.get(group_id, group_id))
        group_hist = get_group_history(group_id, 20)
        last_user_msg = ""
        for m in reversed(group_hist):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "")
                break
        if last_user_msg and group_id in QQ_MEMORY_GROUP_IDS:
            submit_background(write_mem0_chat, last_user_msg, display_reply)
        elif last_user_msg:
            print(f"⏭️ [QQ群{group_label}] 群 {group_id} 不在 QQ_MEMORY_GROUP_IDS 白名单内，跳过写入 Mem0/Pinecone")
        segments = _build_message_segments(reply)
        _log_tag_send(f"QQ群{group_label}", reply, segments)
        if segments is None:
            _completed = True
            return
        await send_qq_msg("group", int(group_id), segments)
        print(f"💬 QQ群[{group_label}] 回复: {display_reply[:60]}")
        _completed = True
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"❌ QQ 群聊回复错误 [group={group_id}]: {type(e).__name__}: {e}", flush=True)
        _completed = True  # 报错也视为完成，确保 finally 补触发积压消息
    finally:
        if _group_pending.get(group_id) is current_task:
            _group_pending.pop(group_id, None)
            if _completed and delay_range[0] >= 8:
                current_hist = get_group_history(group_id)
                if (len(current_hist) > _hist_len_at_reply
                        and current_hist[-1].get("role") == "user"):
                    group_label = f"{group_id}-{_group_names.get(group_id, group_id)}"
                    print(f"💬 QQ群[{group_label}] 检测到未回复消息，补触发一次")
                    _group_pending[group_id] = asyncio.create_task(_group_reply(group_id, (3, 5)))
 
 
async def _poke_reply(group_id: str | None, poker_name: str, poker_id: int):
    try:
        await asyncio.sleep(random.uniform(1.0, 3.0))
        from qq_bot import send_qq_msg, send_poke
 
        if group_id:
            existing = _group_pending.get(group_id)
            if existing and not existing.done():
                existing.cancel()
                _group_pending.pop(group_id, None)
            owner_name_in_group = _member_names.get(group_id, {}).get(QQ_OWNER_ID, "")
            system_prompt = await asyncio.to_thread(build_group_context, owner_name_in_group)
            system_prompt += _GROUP_EXTRA_DEFAULT.format(bot_name=QQ_BOT_NAME)
            system_prompt += _TAG_INSTRUCTIONS_REPLY
            member_block = _build_member_list_block(group_id)
            if member_block:
                system_prompt += _TAG_INSTRUCTIONS_AT + "\n\n" + member_block
            history = get_group_history(group_id, 10, with_ids=True)
        else:
            system_prompt = await asyncio.to_thread(build_qq_context)
            system_prompt += _TAG_INSTRUCTIONS_REPLY
            history = get_chat_history_messages(10, with_ids=True)
 
        system_prompt += f"\n\n【戳一戳】{poker_name}用QQ的戳一戳功能戳了戳你（{QQ_BOT_NAME}）。结合上面的聊天记录，用符合当前氛围的方式自然回应，简短一两句，可以撒娇、调侃、假装被吓到等。必须回应，不允许输出 PASS。回复不要带名字前缀。"
 
        poke_msg = f"{poker_name}戳了戳你"
        messages = [{"role": "system", "content": system_prompt}] + history + [
            {"role": "user", "content": poke_msg}
        ]
        content, _ = await call_llm(messages)
        reply = _strip_name_prefix(_normalize_id_tag(content.strip() if content else ""))
        if not reply or reply == "PASS":
            return
        if group_id:
            reply = sanitize_group_reply(reply, label=f"QQ戳一戳-群{group_id}")
            if not reply:
                return
        display_reply = _strip_message_tags(reply)
        if not display_reply:
            return
        segments = _build_message_segments(reply)
        _log_tag_send(f"QQ戳一戳-{'群' if group_id else '私聊'}", reply, segments)
        if segments is None:
            return
 
        if group_id:
            _gname = _group_names.get(group_id, group_id)
            save_group_message(group_id, "user", poker_name, poke_msg, source=_gname)
            save_group_message(group_id, "assistant", QQ_BOT_NAME, display_reply, source=_gname)
            await send_qq_msg("group", int(group_id), segments)
        else:
            save_chat_message("user", f"[QQ] {poke_msg}")
            save_chat_message("assistant", display_reply)
            await send_qq_msg("private", poker_id, segments)
 
        if random.random() < 0.5:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await send_poke(poker_id, int(group_id) if group_id else None)
 
    except Exception as e:
        print(f"❌ QQ 戳一戳回复错误: {e}")
 
 
async def handle_qq_event(data: dict):
    global _private_pending
    post_type = data.get("post_type")
 
    if _cached_get_setting("qq_bot_paused") == "true":
        return
 
    if post_type == "notice":
        if data.get("notice_type") == "notify" and data.get("sub_type") == "poke":
            target_id = str(data.get("target_id", ""))
            if target_id != QQ_BOT_ID:
                return
            user_id   = data.get("user_id", 0)
            group_id  = str(data.get("group_id", "")) if data.get("group_id") else None
            poker_name = _member_names.get(group_id or "", {}).get(str(user_id), str(user_id))
            group_name = _group_names.get(group_id or "", "")
            location   = f"群{group_id}-{group_name}" if group_id else "私聊"
            print(f"👉 被戳了一戳: {poker_name} ({location})")
            track_task(asyncio.create_task(_poke_reply(group_id, poker_name, user_id)))
        return
 
    if post_type != "message":
        return
 
    message_type = data.get("message_type", "")
    message      = data.get("raw_message", "") or ""
    user_id      = str(data.get("user_id", ""))
    sender       = data.get("sender", {})
    message_list = data.get("message", [])
    message_id   = data.get("message_id")
 
    if message_type == "private":
        if QQ_OWNER_ID and user_id != QQ_OWNER_ID:
            return
        clean = _strip_cq(message)
 
        voice_segs = [seg for seg in message_list if seg.get("type") == "record"]
        if voice_segs:
            voice_url = voice_segs[0].get("data", {}).get("url", "")
            if voice_url:
                recognized = await recognize_qq_voice(voice_url)
                if recognized:
                    print(f"🎤 [QQ语音] 识别结果: {recognized}")
                    save_chat_message("user", f"[QQ语音] {recognized}", message_id=message_id)
                    if _private_pending and not _private_pending.done():
                        _private_pending.cancel()
                    _private_pending = asyncio.create_task(_private_reply())
                else:
                    print("⚠️ [QQ语音] 识别结果为空，跳过")
            return
 
        image_urls = _extract_image_urls(message_list)
        if image_urls:
            try:
                img_desc = await recognize_image_url(image_urls[0], clean or "")
                if not img_desc:
                    raise ValueError("识图返回内容为空")
                img_caption = f"，配文：{clean}" if clean else ""
                clean = f"[图片{img_caption}，视觉识别：{img_desc}]"
                print(f"🖼️ [QQ私聊] 识图完成: {img_desc[:40]}")
            except Exception as e:
                print(f"❌ QQ私聊识图失败: {type(e).__name__}: {e}")
                clean = f"[图片，配文：{clean}](识别失败)" if clean else "[图片](识别失败)"
 
        reply_prefix = await _extract_reply_text(message_list)
        if reply_prefix:
            clean = f"{reply_prefix} {clean}" if clean else reply_prefix
        if not clean:
            return
        print(f"📨 [QQ私聊] {clean[:80]}")
        save_chat_message("user", f"[QQ] {clean}", message_id=message_id)
        if _private_pending and not _private_pending.done():
            _private_pending.cancel()
        _private_pending = asyncio.create_task(_private_reply())
 
    elif message_type == "group":
        group_id = str(data.get("group_id", ""))
        if QQ_GROUP_IDS and group_id not in QQ_GROUP_IDS:
            return
 
        sender_name = sender.get("card") or sender.get("nickname", "未知")
        group_name  = data.get("group_name", "")
        if group_name:
            _group_names[group_id] = group_name
        elif group_id not in _group_names:
            track_task(asyncio.create_task(_fetch_group_name(group_id)))
 
        group_label = f"{group_id}-{_group_names.get(group_id, group_id)}"
 
        _member_names.setdefault(group_id, {})
        prev_name = _member_names[group_id].get(user_id)
        _member_names[group_id][user_id] = sender_name
        _member_names[group_id][QQ_BOT_ID] = QQ_BOT_NAME
        if prev_name != sender_name:
            submit_background(_persist_member_name, group_id, user_id, sender_name)
 
        is_at_bot = f"[CQ:at,qq={QQ_BOT_ID}]" in message
 
        for at_id in _AT_RE.findall(message):
            if at_id != QQ_BOT_ID and at_id not in _member_names.get(group_id, {}):
                track_task(asyncio.create_task(_ensure_member_name(group_id, at_id)))
 
        clean = re.sub(r'\[CQ:at,qq=(\d+)[^\]]*\]', lambda m: _resolve_at(group_id, m.group(1)), message)
        clean = _strip_cq(clean).strip()
 
        if _check_mute(clean, user_id):
            from qq_bot import send_qq_msg
            await send_qq_msg("group", int(group_id), f"好的🐱，我去角落待{MUTE_DURATION}分钟。")
            return
 
        image_urls = _extract_image_urls(message_list)
        if image_urls:
            try:
                img_desc = await recognize_image_url(image_urls[0], clean or "")
                if not img_desc:
                    raise ValueError("识图返回内容为空")
                img_caption = f"，配文：{clean}" if clean else ""
                clean = f"[图片{img_caption}，视觉识别：{img_desc}]"
                print(f"🖼️ [QQ群{group_label}] 识图完成: {img_desc[:40]}")
            except Exception as e:
                print(f"❌ QQ群聊识图失败: {type(e).__name__}: {e}")
                clean = f"[图片，配文：{clean}](识别失败)" if clean else "[图片](识别失败)"
        elif not clean:
            clean = "（发了个表情）"
 
        reply_prefix = await _extract_reply_text(message_list)
        if reply_prefix:
            clean = f"{reply_prefix} {clean}"
 
        if not is_at_bot and _is_muted():
            save_group_message(group_id, "user", sender_name, clean, source=_group_names.get(group_id, group_id), message_id=message_id)
            return
 
        save_group_message(group_id, "user", sender_name, clean, source=_group_names.get(group_id, group_id), message_id=message_id)
        print(f"💬 [QQ群{group_label}] {sender_name}: {clean[:60]}")
 
        if is_at_bot:
            existing = _group_pending.get(group_id)
            if existing and not existing.done():
                existing.cancel()
            _group_pending[group_id] = asyncio.create_task(
                _group_reply(group_id, (2, 4), force_reply=True)
            )
        else:
            existing = _group_pending.get(group_id)
            if image_urls:
                if existing and not existing.done():
                    existing.cancel()
                _group_pending[group_id] = asyncio.create_task(_group_reply(group_id, (3, 5)))
            else:
                if existing and not existing.done():
                    return
                _group_pending[group_id] = asyncio.create_task(_group_reply(group_id))
