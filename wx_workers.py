import os
import time
import asyncio
import json
import random
import traceback
import requests
from datetime import datetime, timezone

from utils import call_llm
from context import (
    build_bot_context,
    save_wx_message,
    get_wx_history_messages,
    get_wx_history_messages_db,
    get_time_context,
)
from mem0_client import search_mem0_context, write_mem0_chat
from bg_executor import submit_background
from workers import _parse_and_write_reminders, DIARY_TRIGGER_PROMPT
from secret_diary import TOOL_DEFINITION as SECRET_DIARY_TOOL, execute_tool as execute_diary_tool

WX_OWNER_ID  = os.environ.get("WX_OWNER_ID", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_ALL_TOOLS = [SECRET_DIARY_TOOL]

_context_token_cache: dict[str, tuple[str, float]] = {}  # value: (token, received_at_epoch)
_private_pending: asyncio.Task | None = None

_CONTEXT_TOKEN_WINDOW_HOURS = 23.5  # 留 0.5h 缓冲，避免卡在 24h 边界发送失败


def _persist_context_token(token: str):
    """把 context_token 和获取时间写入 Supabase bot_settings，供服务重启后恢复"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        for key, value in (("wx_context_token", token), ("wx_context_token_at", now_iso)):
            existing = requests.get(
                f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.{key}&select=key",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                timeout=5,
            ).json()
            if existing:
                requests.patch(
                    f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.{key}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json"},
                    json={"value": value}, timeout=5,
                )
            else:
                requests.post(
                    f"{SUPABASE_URL}/rest/v1/bot_settings",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Content-Type": "application/json", "Prefer": "return=representation"},
                    json={"key": key, "value": value}, timeout=5,
                )
    except Exception as e:
        print(f"⚠️ [WX] context_token 持久化失败: {type(e).__name__}: {e}")
        traceback.print_exc()


def _restore_context_token_cache():
    """服务重启时从 Supabase 恢复 context_token，超过窗口期的不恢复"""
    if not SUPABASE_URL or not SUPABASE_KEY or not WX_OWNER_ID:
        print("📦 [WX] 未配置 SUPABASE 或 WX_OWNER_ID，跳过 context_token 恢复")
        return
    try:
        token_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.wx_context_token&select=value",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=5,
        ).json()
        at_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.wx_context_token_at&select=value",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=5,
        ).json()
        if not token_resp or not at_resp:
            print("📦 [WX] Supabase 无历史 context_token，等待猫猫发消息")
            return
        token  = token_resp[0].get("value", "")
        at_str = at_resp[0].get("value", "")
        if not token or not at_str:
            return
        ts = datetime.fromisoformat(at_str).timestamp()
        age_hours = (time.time() - ts) / 3600
        if age_hours > _CONTEXT_TOKEN_WINDOW_HOURS:
            print(f"📦 [WX] 历史 context_token 已超窗口期（{age_hours:.1f}h），不恢复，等待猫猫发消息")
            return
        _context_token_cache[WX_OWNER_ID] = (token, ts)
        print(f"📦 [WX] 已恢复 context_token，距上次消息 {age_hours:.1f} 小时")
    except Exception as e:
        print(f"⚠️ [WX] 恢复 context_token 失败: {type(e).__name__}: {e}")
        traceback.print_exc()


def _get_valid_context_token(user_id: str) -> str:
    """获取仍在窗口期内的 context_token，过期或不存在返回空字符串"""
    cached = _context_token_cache.get(user_id)
    if not cached:
        return ""
    token, ts = cached
    if (time.time() - ts) / 3600 > _CONTEXT_TOKEN_WINDOW_HOURS:
        return ""
    return token


def _fetch_valid_context_token_db() -> str:
    """_get_valid_context_token 的 Supabase 直查版本。

    2026-07-14 消息进程/后台进程拆分记录：_context_token_cache 这个内存字典
    只在消息进程（Process A）收到微信消息时才会更新（handle_wx_message 里
    _context_token_cache[from_user_id] = (context_token, time.time())）。
    async_wx_proactive_thinking 挪去后台进程（Process B）之后，是完全独立
    的操作系统进程，永远看不到 Process A 内存里这份缓存的更新——只能靠
    Process A 那边 submit_background(_persist_context_token, ...) 写进
    Supabase bot_settings 的 wx_context_token/wx_context_token_at 这两条，
    每次主动思考醒来时现查一次，逻辑（窗口期判断）跟 _restore_context_token_
    cache 保持一致。"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return ""
    try:
        token_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.wx_context_token&select=value",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=5,
        ).json()
        at_resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.wx_context_token_at&select=value",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=5,
        ).json()
        if not token_resp or not at_resp:
            return ""
        token  = token_resp[0].get("value", "")
        at_str = at_resp[0].get("value", "")
        if not token or not at_str:
            return ""
        ts = datetime.fromisoformat(at_str).timestamp()
        age_hours = (time.time() - ts) / 3600
        if age_hours > _CONTEXT_TOKEN_WINDOW_HOURS:
            return ""
        return token
    except Exception as e:
        print(f"⚠️ [WX主动] 查询 context_token 失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        return ""


def _extract_text(msg: dict) -> str:
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            try:
                text = (item.get("text_item") or {}).get("text", "").strip()
                if text:
                    return text
            except Exception as e:
                print(f"❌ [WX] 提取文本异常: {type(e).__name__}: {e} item={item}")
                traceback.print_exc()
    return ""


def _extract_wx_ref(items: list) -> str:
    """从 item_list 提取引用消息文字"""
    for item in items:
        ref = item.get("ref_msg") or {}
        title = ref.get("title", "").strip()
        if title:
            return f"[引用：{title}]"
        ref_msg_item = ref.get("message_item") or {}
        if ref_msg_item.get("type") == 1:
            text = (ref_msg_item.get("text_item") or {}).get("text", "").strip()
            if text:
                return f"[引用：{text[:50]}]"
    return ""


def _split_reply(text: str) -> list[str]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) <= 1:
        return [text.strip()] if text.strip() else []
    merged, buf = [], ""
    for ln in lines:
        if buf and len(buf) < 20 and len(buf + ln) < 80:
            buf += "\n" + ln
        else:
            if buf:
                merged.append(buf)
            buf = ln
    if buf:
        merged.append(buf)
    return merged


async def _delayed_reply(from_user_id: str):
    global _private_pending
    try:
        await asyncio.sleep(random.randint(5, 15))

        context_token = _get_valid_context_token(from_user_id)
        if not context_token:
            print(f"❌ [WX] 找不到 {from_user_id} 的有效 context_token（不存在或已超窗口期），无法回复")
            return

        system_prompt = await asyncio.to_thread(build_bot_context, include_wx_cross=False)
        system_prompt += DIARY_TRIGGER_PROMPT
        system_prompt += "\n\n⚠️ 当前是微信私聊消息，回复风格同私聊。"

        history = get_wx_history_messages(50)
        last_user_text = ""
        for m in reversed(history):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    last_user_text = content
                break

        try:
            mem0_ctx = await asyncio.to_thread(search_mem0_context, last_user_text, 3)
            if mem0_ctx:
                system_prompt += "\n\n" + mem0_ctx
        except Exception as e:
            print(f"❌ [WX] mem0 查询失败: {type(e).__name__}: {e}")
            traceback.print_exc()

        messages = [{"role": "system", "content": system_prompt}] + history

        final_reply = ""
        try:
            for _ in range(6):
                content, tool_calls = await call_llm(messages, tools=_ALL_TOOLS)
                if not tool_calls:
                    final_reply = content
                    break

                assistant_msg: dict = {"role": "assistant"}
                if content:
                    assistant_msg["content"] = content
                assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError as je:
                        print(f"❌ [WX] tool 参数解析失败: fn={fn_name} err={je} raw={tc['function']['arguments'][:100]}")
                        fn_args = {}

                    try:
                        if fn_name == "secret_diary":
                            print("🔒 [WX] 偷写日记...")
                            result = await asyncio.to_thread(execute_diary_tool, fn_args)
                        elif fn_name in FREE_TOOL_DISPATCH:
                            result = await asyncio.to_thread(FREE_TOOL_DISPATCH[fn_name], fn_args)
                        else:
                            result = f"未知工具: {fn_name}"
                    except Exception as te:
                        print(f"❌ [WX] 工具执行异常: fn={fn_name} err={type(te).__name__}: {te}")
                        traceback.print_exc()
                        result = f"工具执行失败: {te}"

                    messages.append({
                        "role":         "tool",
                        "tool_call_id": tc["id"],
                        "content":      str(result),
                    })

            if not final_reply:
                print("⚠️ [WX] 工具轮数耗尽，强制输出回复")
                final_reply, _ = await call_llm(messages, tools=None)

        except Exception as e:
            print(f"❌ [WX] call_llm 异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            return

        if not final_reply:
            print("⚠️ [WX] 回复为空，跳过发送")
            return

        try:
            clean_reply = await asyncio.to_thread(_parse_and_write_reminders, final_reply)
        except Exception as e:
            print(f"❌ [WX] 提醒解析失败: {type(e).__name__}: {e}")
            traceback.print_exc()
            clean_reply = final_reply

        if not clean_reply:
            return

        from wx_bot import send_wx_message
        parts = _split_reply(clean_reply)
        sent_parts: list[str] = []
        for i, part in enumerate(parts):
            ok = await send_wx_message(from_user_id, context_token, part)
            if not ok:
                print(f"❌ [WX] 回复第{i + 1}/{len(parts)}段发送失败，停止发送剩余分段")
                break
            sent_parts.append(part)
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(1.0, 2.5))

        if not sent_parts:
            print("⚠️ [WX] 回复一段都没发出去，不写入历史/mem0")
        else:
            sent_text = "\n".join(sent_parts)
            save_wx_message("assistant", sent_text)
            if len(sent_parts) < len(parts):
                print(f"⚠️ [WX] 回复只发出 {len(sent_parts)}/{len(parts)} 段，历史只记录已发出部分")
            if last_user_text:
                submit_background(write_mem0_chat, last_user_text, sent_text)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"❌ [WX] 私聊回复错误: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if _private_pending is asyncio.current_task():
            _private_pending = None


async def handle_wx_message(msg: dict):
    global _private_pending

    try:
        message_type  = msg.get("message_type", 0)
        from_user_id  = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")
        group_id      = msg.get("group_id", "")

        if message_type == 2:
            return

        if group_id:
            return

        if not from_user_id:
            print("⚠️ [WX] 收到 from_user_id 为空的消息，忽略")
            return

        if WX_OWNER_ID and from_user_id != WX_OWNER_ID:
            print(f"⏭️  [WX] 非 owner 消息，忽略: from={from_user_id}")
            return

        if not WX_OWNER_ID:
            print(f"💡 [WX] 收到消息 from_user_id={from_user_id} （请配置 WX_OWNER_ID={from_user_id} 到 Zeabur 环境变量）")

        if context_token:
            _context_token_cache[from_user_id] = (context_token, time.time())
            submit_background(_persist_context_token, context_token)

        items = msg.get("item_list") or []

        # 提取引用消息（任意 item 里的 ref_msg）
        ref_text = _extract_wx_ref(items)

        # 提取纯文字（type=1）
        text = _extract_text(msg)

        # 处理图片（type=2）
        image_items = [i for i in items if i.get("type") == 2]
        if image_items:
            raw_ii    = image_items[0]
            image_item = raw_ii.get("image_item") or raw_ii
            try:
                from utils import recognize_wx_image
                img_desc = await recognize_wx_image(image_item, text or "")
                if not img_desc:
                    raise ValueError("识图返回内容为空")
                caption  = f"，配文：{text}" if text else ""
                text     = f"[图片{caption}，视觉识别：{img_desc}]"
                print(f"🖼️ [WX私聊] 识图完成: {img_desc[:40]}")
            except Exception as e:
                print(f"❌ [WX] 识图失败: {type(e).__name__}: {e}")
                traceback.print_exc()
                text = f"[图片，配文：{text}](识别失败)" if text else "[图片](识别失败)"

        # 处理语音（type=3）
        voice_items = [i for i in items if i.get("type") == 3]
        if voice_items:
            raw_vi     = voice_items[0]
            voice_item = raw_vi.get("voice_item") or raw_vi
            import json as _json
            raw_str = _json.dumps(raw_vi, ensure_ascii=False)
            chunk = 300
            for i in range(0, len(raw_str), chunk):
                print(f"🔍 [WX语音RAW {i//chunk+1}] {raw_str[i:i+chunk]}")
            try:
                from utils import recognize_wx_voice
                recognized = await recognize_wx_voice(voice_item)
                if recognized:
                    print(f"🎤 [WX语音] 识别结果: {recognized[:60]}")
                    text = f"[微信语音] {recognized}"
                else:
                    print("⚠️ [WX语音] 识别结果为空，跳过")
                    return
            except Exception as e:
                print(f"❌ [WX] 语音识别失败: {type(e).__name__}: {e}")
                traceback.print_exc()
                return

        # 拼接引用前缀
        if ref_text:
            text = f"{ref_text} {text}" if text else ref_text

        if not text:
            print(f"[WX] 消息内容为空，跳过 from={from_user_id} item_types={[i.get('type') for i in items]}")
            return

        print(f"📨 [WX私聊] {text[:60]}")
        save_wx_message("user", f"[微信] {text}")

        if _private_pending and not _private_pending.done():
            _private_pending.cancel()
        _private_pending = asyncio.create_task(_delayed_reply(from_user_id))

    except Exception as e:
        print(f"❌ [WX] handle_wx_message 异常: {type(e).__name__}: {e} msg_keys={list(msg.keys())}")
        traceback.print_exc()


async def async_wx_proactive_thinking():
    """微信版主动思考：复刻 workers.py 的 async_proactive_thinking。
    区别：发送前必须确认 context_token 仍在窗口期内，过期直接跳过本轮，不调用 LLM。"""
    from supabase import create_client
    from context import _sb_exec

    if not WX_OWNER_ID:
        print("⏭️  [WX主动] 未配置 WX_OWNER_ID，跳过微信主动思考线程")
        return

    print("💭 [WX主动] 微信主动思考线程已启动")

    while True:
        wait = random.randint(900, 5400)
        await asyncio.sleep(wait)

        try:
            context_token = await asyncio.to_thread(_fetch_valid_context_token_db)
            if not context_token:
                print("🔕 [WX主动] context_token 不存在或已超窗口期，跳过本轮（不调用LLM）")
                continue

            sb = create_client(SUPABASE_URL, SUPABASE_KEY)

            def _fetch_persona():
                rows = _sb_exec(lambda: sb.table("persona_profile").select("content").execute().data,
                                 label="async_wx_proactive_thinking/persona")
                return rows[0]["content"] if rows else ""

            def _fetch_mem(layer, lim):
                return _sb_exec(lambda: sb.table("memories").select("content")
                                 .eq("memory_layer", layer).order("importance", desc=True)
                                 .limit(lim).execute().data,
                                 label=f"async_wx_proactive_thinking/mem-{layer}")

            def _fetch_phone():
                from context import _get_device_data
                return _get_device_data(sb)

            def _fetch_schedule():
                from context import _get_work_schedule
                return _get_work_schedule(sb)

            def _fetch_platform():
                from context import _get_platform_rolling_summary
                return _get_platform_rolling_summary()

            persona_text, core_rows, current_rows, longterm_rows, phone_text, schedule_text, platform_text = \
                await asyncio.gather(
                    asyncio.to_thread(_fetch_persona),
                    asyncio.to_thread(_fetch_mem, "core", 6),
                    asyncio.to_thread(_fetch_mem, "current", 4),
                    asyncio.to_thread(_fetch_mem, "long_term", 3),
                    asyncio.to_thread(_fetch_phone),
                    asyncio.to_thread(_fetch_schedule),
                    asyncio.to_thread(_fetch_platform),
                )

            history = get_wx_history_messages_db(50)

            mem_parts = []
            for rows, label in [(core_rows, "核心记忆"), (current_rows, "近期状态"),
                                (longterm_rows, "长期记忆")]:
                if rows:
                    mem_parts.append(f"【{label}】\n" + "\n".join(
                        f"- {r['content']}" for r in rows))
            mem_text = "\n\n".join(mem_parts) if mem_parts else "无"

            schedule_block = f"\n\n{schedule_text}" if schedule_text else ""
            platform_block = f"\n\n{platform_text}" if platform_text else ""
            system_content = (
                f"{persona_text}\n\n"
                "现在是后台自动触发的主动思考时刻，场景是微信私聊。\n\n"
                f"{get_time_context()}\n\n"
                f"{mem_text}\n\n"
                f"{phone_text}"
                f"{schedule_block}"
                f"{platform_block}\n\n"
                "⚠️ 上面的全平台近期动向不是无关的背景资料，是猫猫在QQ/TG/微信各处正在"
                "经历的真实的事。判断要不要主动发消息、发什么内容、什么语气时，要考虑"
                "到这些动向——比如她刚在别处心情不好，就不要用轻松无关的话题去打扰；"
                "如果发生了什么值得关心或呼应的事，可以自然地提起。\n\n"
                "请根据以上人格、记忆、猫猫当前状态，以及你们的微信对话历史，"
                "判断此刻要不要主动发一条微信消息给猫猫。\n"
                "如果要发，输出：SEND\n消息内容\n"
                "如果不发，输出：PASS"
            )

            system_msg = {"role": "system", "content": system_content}

            if history and history[-1]["role"] == "user":
                messages = [system_msg] + history
            else:
                messages = [system_msg] + history + [
                    {"role": "user", "content": "（系统触发，非猫猫发送）"}]

            result, _ = await call_llm(messages, max_tokens=2000)

            stripped = result.strip()
            if stripped.startswith("SEND\n") or stripped == "SEND":
                content = stripped[5:].strip() if len(stripped) > 4 else ""
                if content:
                    clean = await asyncio.to_thread(_parse_and_write_reminders, content)
                    if clean:
                        from wx_bot import send_wx_message
                        ok = await send_wx_message(WX_OWNER_ID, context_token, clean)
                        if ok:
                            save_wx_message("assistant", clean)
                            print(f"💬 [WX主动] 发送: {clean[:40]}...")
                        else:
                            print(f"❌ [WX主动] 发送失败，不写入历史: {clean[:40]}...")
            else:
                print("🔕 [WX主动] PASS")

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"❌ [WX主动] 错误: {type(e).__name__}: {e}")
            traceback.print_exc()
