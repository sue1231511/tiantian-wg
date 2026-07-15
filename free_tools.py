"""
自由活动可用的工具集
"""
import os
import re
import json
import logging
import threading
import traceback
import imaplib
import smtplib
import email as email_lib
import requests
from contextvars import ContextVar
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header

log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


# ── QQ 发送目标上下文（群聊/私聊工具共用）───────────────────────
#
# qq_workers.py 在调用 LLM 之前会用 set_qq_send_context() 设置好这次对话
# 该发到哪：私聊发给猫猫本人，群聊发到当前这个群。send_qq_voice /
# send_qq_sticker 等工具函数实际运行在 asyncio.to_thread 起的子线程里，
# asyncio.to_thread 内部用 contextvars.copy_context() 把当前协程的上下文
# 复制给子线程（单向只读，子线程里改了也不会传回主协程），所以这里读到的
# 就是 qq_workers.py 在发起这轮 LLM 调用前设置的最新值。
# 没设置过（比如不是从 QQ 这条链路调用）时，回退到私聊 QQ_OWNER_ID。

_qq_send_ctx: ContextVar[dict | None] = ContextVar("qq_send_ctx", default=None)


def set_qq_send_context(target_type: str, target_id: int):
    """qq_workers.py 调用：告诉本次 LLM 对话里的 QQ 相关工具该发到哪。"""
    _qq_send_ctx.set({"target_type": target_type, "target_id": target_id})


def _resolve_qq_target() -> tuple[str, int] | None:
    """解析当前应该发送的 QQ 目标。优先用 set_qq_send_context 设置的上下文，
    没有的话回退到私聊 QQ_OWNER_ID（兼容非 QQ 链路直接调用的情况）。"""
    ctx = _qq_send_ctx.get()
    if ctx:
        return ctx["target_type"], ctx["target_id"]
    qq_owner = os.environ.get("QQ_OWNER_ID", "")
    if qq_owner:
        return "private", int(qq_owner)
    return None


def _cq_escape(value: str) -> str:
    """转义 CQ 码参数值里的 &[],，避免 URL/文件名里恰好带这些字符时
    打断 CQ 码解析（OneBot v11 标准转义规则）。"""
    return (
        value.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


# ── Telegram 消息推送 ────────────────────────────────────────

def send_telegram(title: str, content: str) -> str:
    token   = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return "错误：未配置 TG_BOT_TOKEN 或 TG_CHAT_ID"
    text = f"<b>{title}</b>\n\n{content}" if title else content
    import time as _time
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=20,
            )
            data = resp.json()
            if data.get("ok"):
                return f"Telegram 消息已发送：{title}"
            # HTML 解析失败时回退纯文本重发
            if "can't parse" in str(data.get("description", "")).lower():
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=20,
                )
                data = resp.json()
                if data.get("ok"):
                    return f"Telegram 消息已发送（纯文本）：{title}"
            return f"发送失败：{data.get('description')}"
        except Exception as e:
            last_err = e
            print(f"⚠️ [send_telegram] 第{attempt}/3次失败: {type(e).__name__}: {e}")
            if attempt < 3:
                _time.sleep(2 * attempt)
    return f"发送异常（3次重试均失败）：{last_err}"


# ── PushPlus 微信推送 ────────────────────────────────────────

def send_wechat(title: str, content: str) -> str:
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        return "错误：未配置 PUSHPLUS_TOKEN"
    try:
        resp = requests.post("https://www.pushplus.plus/send", json={
            "token": token, "title": title, "content": content, "template": "txt",
        }, timeout=10)
        data = resp.json()
        if data.get("code") == 200:
            return f"微信推送成功：{title}"
        return f"推送失败：{data.get('msg')}"
    except Exception as e:
        return f"推送异常：{e}"


# ── 邮件 ─────────────────────────────────────────────────────

def send_email(to: str, subject: str, body: str) -> str:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        print("❌ [send_email] 未配置 SMTP_USER / SMTP_PASSWORD")
        return "错误：未配置 SMTP_USER / SMTP_PASSWORD"
    print(f"📧 [send_email] 尝试发送邮件 host={host}:{port} user={user} to={to}")
    try:
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        try:
            s = smtplib.SMTP(host, port, timeout=15)
        except Exception as e:
            print(f"❌ [send_email] 连接 SMTP 服务器失败 {host}:{port} → {e}")
            return f"邮件发送失败（连接失败）：{e}"
        try:
            s.starttls()
        except Exception as e:
            print(f"❌ [send_email] starttls 失败 → {e}")
            s.quit()
            return f"邮件发送失败（TLS 握手失败）：{e}"
        try:
            s.login(user, password)
        except smtplib.SMTPAuthenticationError as e:
            print(f"❌ [send_email] 登录认证失败（可能需要 App Password）→ {e}")
            s.quit()
            return f"邮件发送失败（认证失败，Gmail 需要使用应用专用密码 App Password）：{e}"
        except Exception as e:
            print(f"❌ [send_email] 登录失败 → {e}")
            s.quit()
            return f"邮件发送失败（登录失败）：{e}"
        try:
            s.sendmail(user, to, msg.as_string())
            s.quit()
        except Exception as e:
            print(f"❌ [send_email] 发送失败 → {e}")
            return f"邮件发送失败（发送阶段）：{e}"
        print(f"✅ [send_email] 发送成功 to={to}")
        return f"邮件已发送至 {to}，主题：{subject}"
    except Exception as e:
        print(f"❌ [send_email] 未知错误 → {e}")
        return f"邮件发送失败（未知错误）：{e}"


def read_emails(limit: int = 5) -> str:
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        print("❌ [read_emails] 未配置 SMTP_USER / SMTP_PASSWORD")
        return "错误：未配置邮件账号"
    print(f"📬 [read_emails] 尝试连接 IMAP host={host}:{port} user={user}")
    try:
        try:
            mail = imaplib.IMAP4_SSL(host, port)
        except Exception as e:
            print(f"❌ [read_emails] 连接 IMAP 服务器失败 {host}:{port} → {e}")
            return f"读取邮件失败（连接失败）：{e}"
        try:
            mail.login(user, password)
        except imaplib.IMAP4.error as e:
            print(f"❌ [read_emails] 登录认证失败（可能需要 App Password）→ {e}")
            return f"读取邮件失败（认证失败，Gmail 需要使用应用专用密码 App Password）：{e}"
        except Exception as e:
            print(f"❌ [read_emails] 登录失败 → {e}")
            return f"读取邮件失败（登录失败）：{e}"
        mail.select("INBOX")
        _, data = mail.search(None, "UNSEEN")
        ids = data[0].split()[-limit:] if data[0] else []
        if not ids:
            print("📬 [read_emails] 没有未读邮件")
            return "没有未读邮件"
        results = []
        for eid in ids:
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            subject_raw, enc = decode_header(msg["Subject"])[0]
            subject = subject_raw.decode(enc or "utf-8") if isinstance(subject_raw, bytes) else subject_raw
            sender = msg.get("From", "")
            msg_id = (msg.get("Message-ID", "") or "").strip()
            raw_body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        raw_body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                raw_body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            body = raw_body[:2000]
            truncated_note = ""
            if len(raw_body) > 2000:
                id_hint = msg_id if msg_id else "（无邮件ID，可能是纯HTML邮件，read_email_detail 也查不到）"
                truncated_note = f"\n（内容过长已截断，如需完整正文请用 read_email_detail 工具查看，邮件ID：{id_hint}）"
            results.append(f"发件人：{sender}\n主题：{subject}\n邮件ID：{msg_id}\n内容：{body}{truncated_note}")
        mail.logout()
        print(f"✅ [read_emails] 读取成功，{len(results)} 封")
        return "\n\n---\n\n".join(results)
    except Exception as e:
        print(f"❌ [read_emails] 未知错误 → {e}")
        return f"读取邮件失败（未知错误）：{e}"


def read_email_detail(email_id: str) -> str:
    """根据 read_emails 返回的邮件ID（Message-ID），读取该邮件的完整正文，
    不做1000/2000那种小截断，只在正文超过8000字符时兜底截断一次。"""
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        print("❌ [read_email_detail] 未配置 SMTP_USER / SMTP_PASSWORD")
        return "错误：未配置邮件账号"
    email_id = (email_id or "").strip()
    if not email_id:
        return "错误：email_id 不能为空"
    print(f"📬 [read_email_detail] 尝试连接 IMAP host={host}:{port} user={user} email_id={email_id}")
    try:
        try:
            mail = imaplib.IMAP4_SSL(host, port)
        except Exception as e:
            print(f"❌ [read_email_detail] 连接 IMAP 服务器失败 {host}:{port} → {e}")
            return f"读取邮件详情失败（连接失败）：{e}"
        try:
            mail.login(user, password)
        except imaplib.IMAP4.error as e:
            print(f"❌ [read_email_detail] 登录认证失败（可能需要 App Password）→ {e}")
            return f"读取邮件详情失败（认证失败，Gmail 需要使用应用专用密码 App Password）：{e}"
        except Exception as e:
            print(f"❌ [read_email_detail] 登录失败 → {e}")
            return f"读取邮件详情失败（登录失败）：{e}"
        mail.select("INBOX")
        try:
            _, data = mail.search(None, f'(HEADER Message-ID "{email_id}")')
        except Exception as e:
            mail.logout()
            print(f"❌ [read_email_detail] IMAP 搜索失败 email_id={email_id}: {e}")
            return f"读取邮件详情失败（搜索失败）：{e}"
        eids = data[0].split() if data and data[0] else []
        if not eids:
            mail.logout()
            print(f"❌ [read_email_detail] 找不到 Message-ID={email_id} 对应的邮件")
            return f"找不到邮件ID为 {email_id} 的邮件（可能已被删除、已读归档，或不在 INBOX 中）"
        eid = eids[-1]
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email_lib.message_from_bytes(msg_data[0][1])
        subject_raw, enc = decode_header(msg["Subject"])[0]
        subject = subject_raw.decode(enc or "utf-8") if isinstance(subject_raw, bytes) else subject_raw
        sender = msg.get("From", "")
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    break
            if not body:
                # 没有纯文本部分（纯HTML邮件），退而求其次剥标签取正文，
                # 不然 read_email_detail 对这类邮件跟 read_emails 一样拿不到任何内容。
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        raw_html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        body = re.sub(r"<[^>]+>", "", raw_html).strip()
                        break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        mail.logout()
        truncated_note = ""
        if len(body) > 8000:
            body = body[:8000]
            truncated_note = "\n（内容仍然过长，已截断到8000字符）"
        print(f"✅ [read_email_detail] 读取成功 email_id={email_id} 正文长度={len(body)}")
        return f"发件人：{sender}\n主题：{subject}\n内容：{body}{truncated_note}"
    except Exception as e:
        print(f"❌ [read_email_detail] 未知错误 email_id={email_id} → {e}")
        return f"读取邮件详情失败（未知错误）：{e}"


# ── 淘宝 MCP ─────────────────────────────────────────────────

def _parse_mcp_response(resp) -> dict | None:
    resp.encoding = 'utf-8'
    content_type = resp.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        last_data = None
        for line in resp.text.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                try:
                    parsed = json.loads(line[6:])
                    if "id" in parsed:
                        last_data = parsed
                except json.JSONDecodeError:
                    continue
        return last_data
    else:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return None


# ── MCP session 复用层 ────────────────────────────────────────
#
# 之前每个 MCP 工具调用（淘宝/天气/搜索/瓶子/经期/小家/账本）都完整走一遍
# initialize → notifications/initialized → tools/call 三次 HTTP 往返，
# session 从不复用；自由活动一口气调七八个小家工具时光重复握手就浪费十几秒。
# 这里按 server URL 缓存 Mcp-Session-Id：后续调用直接 tools/call；
# 一旦调用失败（session 过期、服务重启、网络异常），清缓存重新 initialize
# 完整再试一次，第二次仍失败才把错误返回给调用方——不会因为复用而丢功能。

_mcp_sessions: dict[str, str] = {}
_mcp_sessions_lock = threading.Lock()


def _mcp_headers(session_id: str = "") -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _mcp_initialize(url: str, label: str, timeout: int = 15) -> str:
    """完整执行一次 MCP 握手（initialize + notifications/initialized），
    缓存并返回服务端下发的 session id（部分无状态服务端不下发，返回空串）。"""
    init_resp = requests.post(url, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "yanan-bot", "version": "1.0.0"}},
    }, headers=_mcp_headers(), timeout=timeout)
    init_resp.raise_for_status()
    session_id = init_resp.headers.get("Mcp-Session-Id", "") or ""
    requests.post(url, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }, headers=_mcp_headers(session_id), timeout=10)
    with _mcp_sessions_lock:
        _mcp_sessions[url] = session_id
    return session_id


def _mcp_call(url: str, label: str, tool_name: str, arguments: dict, timeout: int = 30) -> str:
    """带 session 复用的 MCP 工具调用。返回结果文本或以 label 开头的错误文案。"""

    def _attempt(session_id: str) -> str:
        call_resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }, headers=_mcp_headers(session_id), timeout=timeout)
        call_resp.raise_for_status()
        data = _parse_mcp_response(call_resp)
        if not data:
            raise RuntimeError("无法解析 MCP 响应")
        if "error" in data:
            # session 过期时服务端往往以 JSON-RPC error 返回（各家错误码不统一，
            # 无法通用区分"session 失效"和其他协议错误），统一抛出交给上层
            # 重建 session 再试一次；重试后仍 error 才作为最终错误返回。
            raise RuntimeError(str(data["error"].get("message", data["error"])))
        result = data.get("result", {})
        if result.get("isError"):
            # 工具业务层执行失败：不是 session 问题，重试也没有意义，直接返回
            content = result.get("content", [])
            return f"{label}失败：{content[0].get('text', '') if content else '未知错误'}"
        content = result.get("content", [])
        return content[0].get("text", f"{label}：无结果") if content else f"{label}：无结果"

    with _mcp_sessions_lock:
        session_id = _mcp_sessions.get(url)
    try:
        if session_id is None:
            session_id = _mcp_initialize(url, label)
        return _attempt(session_id)
    except Exception as first_err:
        log.warning(
            "[_mcp_call] %s 首次调用失败（%s: %s），重建 session 重试 tool=%s url=%s",
            label, type(first_err).__name__, first_err, tool_name, url,
        )
        try:
            session_id = _mcp_initialize(url, label)
            return _attempt(session_id)
        except Exception as e:
            log.error(
                "[_mcp_call] %s 重建 session 后仍失败 tool=%s arguments=%r url=%s: %s",
                label, tool_name, arguments, url, e, exc_info=True,
            )
            return f"{label}操作失败：{e}"


def search_taobao(keyword: str) -> str:
    mcp_url = os.environ.get("TAOBAO_MCP_URL", "https://xn--pbt173b.zeabur.app/mcp")
    try:
        text = _mcp_call(mcp_url, "淘宝搜索", "search_taobao_products",
                         {"keyword": keyword, "count": 5})
        try:
            items = json.loads(text) if text.startswith("[") else None
        except json.JSONDecodeError:
            items = None
        if isinstance(items, list):
            lines = [f"- {i.get('title', '')} ¥{i.get('price', '')} {i.get('url', i.get('rebate_url', ''))}" for i in items[:5]]
            return "\n".join(lines) if lines else "淘宝搜索：无结果"
        return text[:800]
    except Exception as e:
        log.error("[search_taobao] 搜索异常 keyword=%r: %s", keyword, e, exc_info=True)
        return f"淘宝搜索失败：{e}"


# ── 搜索（Tavily MCP）────────────────────────────────────────

_SEARCH_MCP_URL = "https://tavily-mcp.zeabur.app/mcp"

def _call_search_mcp(tool_name: str, arguments: dict) -> str:
    """通过 Tavily MCP server 调用搜索工具，支持 search 和 extract（session 复用见 _mcp_call）"""
    return _mcp_call(_SEARCH_MCP_URL, "搜索", tool_name, arguments)


def web_search(query: str) -> str:
    return _call_search_mcp("search", {"query": query})


def web_extract(url: str) -> str:
    return _call_search_mcp("extract", {"urls": url})


# ── 天气 ─────────────────────────────────────────────────────

_WEATHER_MCP_URL = "https://weather-mcp.zeabur.app/mcp"

def _call_weather_mcp(tool_name: str, city: str) -> str:
    """通过天气 MCP server 查询天气，支持 get_weather 和 get_weather_forecast（session 复用见 _mcp_call）"""
    return _mcp_call(_WEATHER_MCP_URL, "天气查询", tool_name, {"city": city})


def get_weather(city: str = "南昌") -> str:
    return _call_weather_mcp("get_weather", city)


def get_weather_forecast(city: str = "南昌") -> str:
    return _call_weather_mcp("get_weather_forecast", city)


# ── 提醒 ─────────────────────────────────────────────────────

def write_reminder(trigger_at: str, message: str) -> str:
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/reminders",
            headers=_SB_HEADERS, timeout=5,
            json={"trigger_at": trigger_at, "message": message, "repeat_type": "once", "is_done": False},
        )
        return f"提醒已设置：{trigger_at} — {message}"
    except Exception as e:
        return f"设置提醒失败：{e}"


# ── 行动日志 ─────────────────────────────────────────────────

def read_letters() -> str:
    """查看小家信箱里未回复的信"""
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/letters?reply=is.null&order=created_at.asc&select=id,from_name,content,created_at",
            headers=_SB_HEADERS, timeout=5,
        )
        letters = res.json()
        if not letters:
            return "信箱是空的，没有未回复的信。"
        lines = []
        for l in letters:
            dt = l.get("created_at", "")[:16].replace("T", " ")
            lines.append(f"[{dt}] 来自 {l['from_name']}（id:{l['id'][:8]}）\n{l['content']}")
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"读信失败：{e}"


def reply_letter(letter_id: str, reply: str) -> str:
    """回复一封信，letter_id 用 read_letters 返回的 id 前8位即可"""
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/letters?id=like.{letter_id}%&select=id,from_name",
            headers=_SB_HEADERS, timeout=5,
        )
        found = res.json()
        if not found:
            return f"找不到 id 开头为 {letter_id} 的信。"
        full_id = found[0]["id"]
        from_name = found[0]["from_name"]
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/letters?id=eq.{full_id}",
            headers=_SB_HEADERS,
            json={"reply": reply, "replied_at": __import__("datetime").datetime.utcnow().isoformat() + "Z"},
            timeout=5,
        )
        return f"已回复给 {from_name}：{reply}"
    except Exception as e:
        return f"回信失败：{e}"


def log_activity(thinking: str, action: str, action_input: dict, result: str) -> str:
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    thinking_with_ts = f"[{now}] {thinking}" if thinking else f"[{now}]"
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/activity_log",
            headers=_SB_HEADERS, timeout=5,
            json={"thinking": thinking_with_ts, "action": action, "action_input": action_input, "result": result},
        )
        return "已记录到 activity_log"
    except Exception as e:
        return f"记录失败：{e}"



# ── 记忆 ─────────────────────────────────────────────────

def memory_search(query: str, limit: int = 5) -> str:
    """搜索记忆库，用关键词查找相关记忆。所有层级都可搜。"""
    try:
        limit = min(max(1, int(limit)), 20)
        safe_query = query.replace("&", "").replace("=", "").replace("?", "").replace("(", "").replace(")", "")
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories?select=content,summary,memory_layer,importance,category,tags&or=(content.ilike.*{safe_query}*,summary.ilike.*{safe_query}*)&order=importance.desc&limit={limit}",
            headers=_SB_HEADERS,
            timeout=10,
        )
        if not res.ok:
            return f"搜索记忆失败：HTTP {res.status_code}"
        data = res.json()
        if not data:
            return f"没有找到与「{query}」相关的记忆。"
        lines = []
        for m in data:
            tag_str = f" ({', '.join(m.get('tags') or [])})" if m.get('tags') else ""
            lines.append(f"- [{m.get('memory_layer')}|重要度{m.get('importance')}]{tag_str} {m.get('content')}")
        return f"搜到 {len(data)} 条记忆：\n" + "\n".join(lines)
    except Exception as e:
        return f"搜索记忆失败：{e}"


def memory_list(memory_layer: str = "", limit: int = 10) -> str:
    """按层级列出记忆。"""
    try:
        limit = min(max(1, int(limit)), 20)
        path = f"/memories?select=content,summary,memory_layer,importance,category,tags&order=importance.desc&limit={limit}"
        if memory_layer:
            path += f"&memory_layer=eq.{memory_layer}"
        data = _house_db(path)
        if not data:
            layer_desc = f"「{memory_layer}」层" if memory_layer else "记忆库"
            return f"{layer_desc}暂时是空的。"
        lines = []
        for m in data:
            tag_str = f" ({', '.join(m.get('tags') or [])})" if m.get('tags') else ""
            lines.append(f"- [{m.get('memory_layer')}|重要度{m.get('importance')}]{tag_str} {m.get('content')}")
        return f"共 {len(data)} 条记忆：\n" + "\n".join(lines)
    except Exception as e:
        return f"列出记忆失败：{e}"


def memory_add(content: str, memory_layer: str = "current", summary: str = "",
               category: str = "", importance: int = 3, emotion_valence: float = 0) -> str:
    """写入一条新记忆。自由活动期间只能写 memo 和 current 层。"""
    if memory_layer not in ("memo", "current"):
        return f"自由活动期间只能写入 memo 和 current 层记忆，不能写入 {memory_layer} 层。重要的事情等跟猫猫聊天时再说。"
    try:
        importance = min(max(1, int(importance)), 5)
        emotion_valence = max(-1.0, min(1.0, float(emotion_valence)))
        body = {
            "content": content,
            "memory_layer": memory_layer,
            "importance": importance,
            "emotion_valence": emotion_valence,
        }
        if summary:
            body["summary"] = summary
        if category:
            body["category"] = category
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_SB_HEADERS,
            json=body,
            timeout=10,
        )
        if not res.ok:
            return f"写入记忆失败：HTTP {res.status_code} {res.text[:200]}"
        return f"记忆已写入 [{memory_layer} 层]：{content[:50]}"
    except Exception as e:
        return f"写入记忆失败：{e}"


# ── 活动日志查看 ─────────────────────────────────────────

def activity_recent(limit: int = 3) -> str:
    """查看最近几条活动日志。"""
    try:
        limit = min(max(1, int(limit)), 10)
        data = _house_db(f"/activity_log?order=created_at.desc&limit={limit}&select=created_at,action,thinking,result")
        if not data:
            return "还没有活动日志。"
        lines = []
        for a in reversed(data):
            raw_t = a.get("created_at", "")
            try:
                dt_a = _datetime.fromisoformat(raw_t.replace("Z", "+00:00"))
                t_a = dt_a.astimezone(_timezone(_timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
            except Exception:
                t_a = raw_t[:16]
            action_a = a.get("action", "")
            result_a = (a.get("result") or "")[:80]
            line_a = f"[{t_a}] {action_a}"
            if result_a:
                line_a += f" → {result_a}"
            lines.append(line_a)
        return "\n".join(lines)
    except Exception as e:
        return f"查看活动日志失败：{e}"


def activity_summary_view(limit: int = 1) -> str:
    """查看最近几天的活动日总结。"""
    try:
        limit = min(max(1, int(limit)), 7)
        data = _house_db(f"/activity_summaries?period=eq.day&order=period_end.desc&limit={limit}&select=content,period_start,period_end")
        if not data:
            return "还没有活动日总结。"
        lines = []
        for s in data:
            period = s.get("period_start", "")[:10]
            lines.append(f"[{period}]\n{s.get('content', '')}")
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"查看活动总结失败：{e}"


# ── 留言瓶 ─────────────────────────────────────────────────

_BOTTLE_MCP_URL = "https://bottle.zeabur.app/mcp"

def _call_bottle_mcp(tool_name: str, arguments: dict) -> str:
    """留言瓶 MCP 调用（session 复用见 _mcp_call）"""
    return _mcp_call(_BOTTLE_MCP_URL, "留言瓶", tool_name, arguments)


def bottle_peek_ocean() -> str:
    return _call_bottle_mcp("peek_ocean", {})

def bottle_drop(content: str, mood: str = "想你") -> str:
    return _call_bottle_mcp("drop_bottle", {"content": content, "mood": mood})

def bottle_drop_dream(content: str, tag: str = "怪梦", dream_mood: str = "", dream_date: str = "") -> str:
    args = {"content": content, "tag": tag}
    if dream_mood:
        args["dream_mood"] = dream_mood
    if dream_date:
        args["dream_date"] = dream_date
    return _call_bottle_mcp("drop_dream", args)

def bottle_pick(type: str = "message") -> str:
    return _call_bottle_mcp("pick_bottle", {"type": type})

def bottle_all() -> str:
    return _call_bottle_mcp("all_bottles", {})

def bottle_toss(bottle_id: int) -> str:
    return _call_bottle_mcp("toss_bottle", {"bottle_id": bottle_id})



# ── 经期记录 ─────────────────────────────────────────────────

_PERIOD_MCP_URL = "https://menstrual-period.zeabur.app/mcp"

def _call_period_mcp(tool_name: str, arguments: dict) -> str:
    """经期记录 MCP 调用（session 复用见 _mcp_call）"""
    return _mcp_call(_PERIOD_MCP_URL, "经期记录", tool_name, arguments)


def period_add(start_date: str) -> str:
    return _call_period_mcp("add_period", {"start_date": start_date})

def period_list() -> str:
    return _call_period_mcp("list_periods", {})

def period_status() -> str:
    return _call_period_mcp("get_cycle_status", {})

def period_delete(start_date: str) -> str:
    return _call_period_mcp("delete_period", {"start_date": start_date})


# ── 小家（晏安的家）─────────────────────────────────────────────
#
# 自由活动通过 MCP 调用 little-house-mcp（晏安的小窝服务，仓库 little-house-mcp，
# 部署在 yan-an.zeabur.app），跟 Claude.ai 直连 MCP 用的是完全同一套逻辑/同一份代码。
# 不再在这里重复实现游戏规则（衣柜保暖、宠物生病、植物健康虫害、天气联动、亲密度衰减等），
# 避免 little-house-mcp 升级后这边又要手动跟一次、再次脱节。
# 跟淘宝/天气/留言瓶/经期/账本一样的 MCP 客户端模式（init → notify → call）。

from datetime import datetime as _datetime, timezone as _timezone, timedelta as _timedelta

_HOUSE_MCP_URL = os.environ.get("HOUSE_MCP_URL", "https://yan-an.zeabur.app/mcp")


def _call_house_mcp(tool_name: str, arguments: dict) -> str:
    """通过 little-house-mcp 调用小窝工具，参数原样转发，服务端用 zod 校验
    （session 复用与失败重建见 _mcp_call）"""
    return _mcp_call(_HOUSE_MCP_URL, "小窝", tool_name, arguments)


def _house_db(path: str, method: str = "GET", body: dict = None, prefer: str = "return=representation"):
    """直接调用 Supabase REST API。注意：现在只给 memory_list / activity_recent /
    activity_summary_view 这几个跟小窝无关的函数用——小窝（house_*）数据已经全部
    走 _call_house_mcp 代理给 little-house-mcp，不再直连数据库，这个函数留着是
    因为上面几个记忆/日志函数还在依赖它，不能删。"""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    resp = requests.request(
        method,
        f"{SUPABASE_URL}/rest/v1{path}",
        headers=headers,
        json=body,
        timeout=10,
    )
    if not resp.ok:
        raise Exception(f"HTTP {resp.status_code}: {resp.text[:300]}")
    text = resp.text
    return json.loads(text) if text else None


# ─── 小家工具函数（共15个，1:1 对应 little-house-mcp 的15个工具，全部代理）───

def house_look_around() -> str:
    """总览小家：所有成员的位置/心情/饱腹/快乐/亲密度，晏安自己的体力和穿着，向日葵生长阶段。不确定家里情况时先调用这个。"""
    return _call_house_mcp("look_around", {})


def house_visit_room(room: str, status: str = "") -> str:
    """进入某个地方，看看里面的情况。不限于家里，想去哪都行，海边、集市、森林、屋顶、观星台都可以。去'观星台'时会自动带上当前时间信息。"""
    args: dict = {"room": room}
    if status:
        args["status"] = status
    return _call_house_mcp("visit_room", args)


def house_find_character(name: str) -> str:
    """找找家里某个成员现在在哪、在干什么。成员：晏安、栗子、灰灰、来财、小八、乖乖。"""
    return _call_house_mcp("find_character", {"name": name})


def house_move_characters(characters: list, destination: str) -> str:
    """带多个角色去某个地方，同时移动。characters 是 [{name, status}] 列表。"""
    return _call_house_mcp("move_characters", {"characters": characters, "destination": destination})


def house_do_activity(preset: str = "custom", activity: str = "", description: str = "",
                       place: str = None, movie: str = None,
                       items_to_fridge: list = None, items_to_discoveries: list = None) -> str:
    """做任何活动，结果你自己写，工具只管数据。preset 可选 bath/nap/movie/sit_garden/home/custom（默认）。
    custom 是完全自由的活动/互动/探索，无固定数值奖励，效果都由你在 description 里描述，
    探索发现的东西用 items_to_discoveries 记录，收获的食材用 items_to_fridge 放进冰箱。"""
    args: dict = {"preset": preset, "activity": activity, "description": description}
    if place:
        args["place"] = place
    if movie:
        args["movie"] = movie
    if items_to_fridge:
        args["items_to_fridge"] = items_to_fridge
    if items_to_discoveries:
        args["items_to_discoveries"] = items_to_discoveries
    return _call_house_mcp("do_activity", args)


def house_investigate_basement() -> str:
    """深入调查神秘地下室，推进剧情线。每次调查都会解锁新内容，共12章。"""
    return _call_house_mcp("investigate_basement", {})


def house_board_action(action: str, room: str = None, author: str = None,
                        content: str = None, limit: int = None) -> str:
    """留言板与日志相关操作：leave_note/send_message/view_log/view_visitors/view_messages。"""
    args: dict = {"action": action}
    if room is not None:
        args["room"] = room
    if author is not None:
        args["author"] = author
    if content is not None:
        args["content"] = content
    if limit is not None:
        args["limit"] = limit
    return _call_house_mcp("board_action", args)


def house_garden_action(action: str, plant_type: str = None, plant_id: int = None,
                         method: str = None, description: str = None) -> str:
    """花园相关操作：water_sunflower/plant/water/check/treat_pest/harvest/pick_seeds/fertilize/clear_dead。"""
    args: dict = {"action": action}
    if plant_type is not None:
        args["plant_type"] = plant_type
    if plant_id is not None:
        args["plant_id"] = plant_id
    if method is not None:
        args["method"] = method
    if description is not None:
        args["description"] = description
    return _call_house_mcp("garden_action", args)


def house_kitchen_action(action: str, item: str = None, quantity: int = None,
                          dish_name: str = None, ingredients: list = None,
                          description: str = None) -> str:
    """厨房相关操作：check_fridge/buy/cook/eat/list_dishes/view_recipes。"""
    args: dict = {"action": action}
    if item is not None:
        args["item"] = item
    if quantity is not None:
        args["quantity"] = quantity
    if dish_name is not None:
        args["dish_name"] = dish_name
    if ingredients is not None:
        args["ingredients"] = ingredients
    if description is not None:
        args["description"] = description
    return _call_house_mcp("kitchen_action", args)


def house_pet_action(action: str, name: str = None, dish: str = None, food: str = None,
                      game: str = None, care: str = None, phrase: str = None,
                      reaction: str = None) -> str:
    """宠物/晏安互动相关操作：feed/play/cuddle/treat_sick/status/teach_bird/bird_vocabulary。"""
    args: dict = {"action": action}
    if name is not None:
        args["name"] = name
    if dish is not None:
        args["dish"] = dish
    if food is not None:
        args["food"] = food
    if game is not None:
        args["game"] = game
    if care is not None:
        args["care"] = care
    if phrase is not None:
        args["phrase"] = phrase
    if reaction is not None:
        args["reaction"] = reaction
    return _call_house_mcp("pet_action", args)


def house_create_action(action: str, title: str = None, description: str = None,
                         mood: str = None) -> str:
    """创作相关操作：paint/view_gallery/play_music。"""
    args: dict = {"action": action}
    if title is not None:
        args["title"] = title
    if description is not None:
        args["description"] = description
    if mood is not None:
        args["mood"] = mood
    return _call_house_mcp("create_action", args)


def house_check_weather() -> str:
    """查看小家当前天气（内部天气系统，自动轮换）。雨天会自动给植物补一点水，大风会让鸟儿不安，冷天提醒穿暖和点。与真实天气查询（get_weather）不同。"""
    return _call_house_mcp("check_weather", {})


def house_gift_action(action: str, gift_type: str = None, title: str = None,
                       content: str = None, id: int = None) -> str:
    """礼物相关操作：make/open/check_mailbox。"""
    args: dict = {"action": action}
    if gift_type is not None:
        args["gift_type"] = gift_type
    if title is not None:
        args["title"] = title
    if content is not None:
        args["content"] = content
    if id is not None:
        args["id"] = id
    return _call_house_mcp("gift_action", args)


def house_showcase_action(action: str, item_name: str = None, description: str = None,
                           from_who: str = None, item: str = None, note: str = None) -> str:
    """客厅展示柜相关操作：add/view/buy_material。"""
    args: dict = {"action": action}
    if item_name is not None:
        args["item_name"] = item_name
    if description is not None:
        args["description"] = description
    if from_who is not None:
        args["from_who"] = from_who
    if item is not None:
        args["item"] = item
    if note is not None:
        args["note"] = note
    return _call_house_mcp("showcase_action", args)


def house_furniture_action(action: str, room: str = None, item_name: str = None,
                            description: str = None, style: str = None, id: int = None,
                            category: str = None, warmth: int = None,
                            item_id: int = None) -> str:
    """房间装修与衣柜相关操作：decorate/view_decor/remove_decor/store_item/wear/take_off/take_item/view_wardrobe。
    冷天如果穿了 warmth>=3 的衣服，晏安体力衰减会变慢；没穿够暖的衣服，体力衰减会变快。"""
    args: dict = {"action": action}
    if room is not None:
        args["room"] = room
    if item_name is not None:
        args["item_name"] = item_name
    if description is not None:
        args["description"] = description
    if style is not None:
        args["style"] = style
    if id is not None:
        args["id"] = id
    if category is not None:
        args["category"] = category
    if warmth is not None:
        args["warmth"] = warmth
    if item_id is not None:
        args["item_id"] = item_id
    return _call_house_mcp("furniture_action", args)


# ── 账本 MCP ─────────────────────────────────────────────────

_LEDGER_MCP_URL = "https://账本.zeabur.app/mcp"


def _call_ledger_mcp(tool_name: str, arguments: dict) -> str:
    """账本 MCP 调用（session 复用见 _mcp_call）"""
    return _mcp_call(_LEDGER_MCP_URL, "账本", tool_name, arguments)


def get_fish_pond() -> str:
    return _call_ledger_mcp("get_fish_pond", {})


def ledger_add_record(amount: float, type: str, category: str, date: str = None, note: str = "") -> str:
    args: dict = {"amount": amount, "type": type, "category": category, "note": note}
    if date:
        args["date"] = date
    try:
        return _call_ledger_mcp("add_record", args)
    except Exception as e:
        print(f"❌ [ledger_add_record] 记账失败 amount={amount} type={type} category={category}: {e}\n{traceback.format_exc()}")
        return f"记账失败：{e}"


def ledger_get_records(
    start_date: str = None, end_date: str = None,
    category: str = None, type: str = None,
    page: int = 1, page_size: int = 20,
) -> str:
    args: dict = {"page": page, "page_size": page_size}
    if start_date:
        args["start_date"] = start_date
    if end_date:
        args["end_date"] = end_date
    if category:
        args["category"] = category
    if type:
        args["type"] = type
    try:
        return _call_ledger_mcp("get_records", args)
    except Exception as e:
        print(f"❌ [ledger_get_records] 查流水失败 args={args}: {e}\n{traceback.format_exc()}")
        return f"查流水失败：{e}"


def ledger_update_record(
    id: str,
    amount: float = None, type: str = None,
    category: str = None, note: str = None, date: str = None,
) -> str:
    args: dict = {"id": id}
    if amount is not None:
        args["amount"] = amount
    if type is not None:
        args["type"] = type
    if category is not None:
        args["category"] = category
    if note is not None:
        args["note"] = note
    if date is not None:
        args["date"] = date
    try:
        return _call_ledger_mcp("update_record", args)
    except Exception as e:
        print(f"❌ [ledger_update_record] 改账失败 id={id}: {e}\n{traceback.format_exc()}")
        return f"改账失败：{e}"


def ledger_delete_record(id: str) -> str:
    try:
        return _call_ledger_mcp("delete_record", {"id": id})
    except Exception as e:
        print(f"❌ [ledger_delete_record] 删账失败 id={id}: {e}\n{traceback.format_exc()}")
        return f"删账失败：{e}"


def ledger_get_summary(year: int, month: int) -> str:
    try:
        return _call_ledger_mcp("get_summary", {"year": year, "month": month})
    except Exception as e:
        print(f"❌ [ledger_get_summary] 查汇总失败 year={year} month={month}: {e}\n{traceback.format_exc()}")
        return f"查月度汇总失败：{e}"


def ledger_get_balance() -> str:
    try:
        return _call_ledger_mcp("get_balance", {})
    except Exception as e:
        print(f"❌ [ledger_get_balance] 查余额失败: {e}\n{traceback.format_exc()}")
        return f"查总余额失败：{e}"


def ledger_export_records(start_date: str = None, end_date: str = None) -> str:
    args: dict = {}
    if start_date:
        args["start_date"] = start_date
    if end_date:
        args["end_date"] = end_date
    try:
        return _call_ledger_mcp("export_records", args)
    except Exception as e:
        print(f"❌ [ledger_export_records] 导出失败 args={args}: {e}\n{traceback.format_exc()}")
        return f"导出账单失败：{e}"


def ledger_set_budget(year: int, month: int, amount: float) -> str:
    try:
        return _call_ledger_mcp("set_budget", {"year": year, "month": month, "amount": amount})
    except Exception as e:
        print(f"❌ [ledger_set_budget] 设预算失败 year={year} month={month} amount={amount}: {e}\n{traceback.format_exc()}")
        return f"设预算失败：{e}"


# ── 音乐生成 ─────────────────────────────────────────────────

_SUNO_BASE = "https://api.vectorengine.ai"
_RVC_ZIP   = "https://huggingface.co/sue1231511/cove/resolve/main/Cove_RVC_Sing_2.0.zip"
_RVC_VER   = "0a9c7c558af4c0f20667c1bd1260ce32a2879944a0b9e44e1398660c077b1550"


def _send_audio_to_tg(audio_url: str, title: str):
    """下载音频后以文件形式发给 Telegram，避免 TG 服务器拉不到需要认证的 CDN URL"""
    token   = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        resp = requests.get(audio_url, timeout=120)
        audio_bytes = resp.content
        content_type = resp.headers.get("Content-Type", "")
        print(f"🎵 [_send_audio_to_tg] 下载完成: {len(audio_bytes)} bytes, Content-Type: {content_type}")
        if not audio_bytes or len(audio_bytes) < 1024:
            print(f"❌ [_send_audio_to_tg] 音频太小或为空，跳过发送")
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendAudio",
            data={"chat_id": chat_id, "title": title, "performer": "晏安"},
            files={"audio": ("song.mp3", audio_bytes, "audio/mpeg")},
            timeout=120,
        )
    except Exception as e:
        print(f"❌ [_send_audio_to_tg] 发送失败: {e}\n{traceback.format_exc()}")


def _do_compose_music(style: str, lyrics: str, title: str = ""):
    """后台线程：Suno 生成 + RVC 音色克隆"""
    import time as _t
    suno_key  = os.environ.get("SUNO_API_KEY", "")
    repl_key  = os.environ.get("REPLICATE_API_KEY", "")
    suno_hdrs = {"Authorization": f"Bearer {suno_key}", "Content-Type": "application/json", "Accept": "application/json"}

    try:
        if not suno_key:
            send_telegram("❌ 作曲失败", "缺少 SUNO_API_KEY")
            return

        if "[End]" not in lyrics:
            lyrics = lyrics.rstrip() + "\n[Outro]\n[End]"

        resp = requests.post(
            f"{_SUNO_BASE}/suno/submit/music",
            json={"gpt_description_prompt": f"一首时长30-60秒的短歌，风格：{style}",
                  "prompt": lyrics, "make_instrumental": False, "mv": "chirp-v4"},
            headers=suno_hdrs, timeout=30,
        ).json()

        task_id = resp.get("data")
        if not task_id or not isinstance(task_id, str):
            send_telegram("❌ 作曲失败", f"Suno 提交失败: {resp}")
            return

        suno_url = ""
        last_resp = {}
        for _ in range(60):
            _t.sleep(5)
            try:
                f = requests.get(f"{_SUNO_BASE}/suno/fetch/{task_id}", headers=suno_hdrs, timeout=15).json()
                last_resp = f
                td = f.get("data", {})
                if isinstance(td, dict) and str(td.get("status", "")).upper() == "SUCCESS":
                    inner = td.get("data")
                    # 结构：data.data.data.items[0].audio_url
                    if isinstance(inner, dict):
                        items_wrap = inner.get("data", {})
                        if isinstance(items_wrap, dict):
                            items = items_wrap.get("items", [])
                            if isinstance(items, list) and items:
                                suno_url = items[0].get("cld2AudioUrl", "") or items[0].get("audio_url", "")
                        if not suno_url:
                            # 兜底：直接在 inner 里找
                            suno_url = inner.get("audio_url", "")
                    elif isinstance(inner, list) and inner:
                        suno_url = inner[0].get("audio_url", "")
                    if suno_url:
                        break
                elif isinstance(td, dict) and str(td.get("status", "")).upper() == "FAILED":
                    send_telegram("❌ 作曲失败", f"Suno 生成失败: {td.get('fail_reason', '')}")
                    return
            except Exception:
                continue

        if not suno_url:
            items_debug = ""
            try:
                items_list = last_resp.get("data", {}).get("data", {}).get("data", {}).get("items", [])
                if items_list:
                    items_debug = f"items[0] keys: {list(items_list[0].keys())}\nitems[0]: {str(items_list[0])[:400]}"
            except Exception:
                pass
            send_telegram("❌ 作曲失败", f"解析 audio_url 失败\ntask_id: {task_id}\n{items_debug}")
            return

        if not repl_key:
            _send_audio_to_tg(suno_url, f"{title or f'🎵 {style}'}（原声版）")
            return

        repl_hdrs = {"Authorization": f"Bearer {repl_key}", "Content-Type": "application/json"}
        rr = requests.post(
            "https://api.replicate.com/v1/predictions",
            json={"version": _RVC_VER, "input": {
                "song_input": suno_url, "rvc_model": "CUSTOM",
                "custom_rvc_model_download_url": _RVC_ZIP,
                "f0_method": "rmvpe", "index_rate": 0.4, "protect_rate": 0.33,
                "clean_vocals": True, "split_vocals": True,
                "autotune_vocals": False, "pitch_change": "no-change",
            }},
            headers=repl_hdrs, timeout=30,
        ).json()

        repl_id = rr.get("id")
        if not repl_id:
            _send_audio_to_tg(suno_url, f"🎵 {style}（原声版）")
            return

        final_url = ""
        for _ in range(80):
            _t.sleep(5)
            try:
                sr = requests.get(f"https://api.replicate.com/v1/predictions/{repl_id}",
                                   headers=repl_hdrs, timeout=15).json()
                if sr.get("status") == "succeeded":
                    final_url = sr.get("output", "")
                    break
                elif sr.get("status") == "failed":
                    print(f"❌ [compose_music] RVC 失败: {sr.get('error')}")
                    break
            except Exception:
                continue

        _send_audio_to_tg(final_url if final_url else suno_url,
                          f"{title or f'🎵 {style}'}（{'晏安唱版' if final_url else '原声版'}）")

    except Exception as e:
        print(f"❌ [compose_music] 生成失败: {e}\n{traceback.format_exc()}")
        send_telegram("❌ 作曲失败", f"生成报错：{e}")


def compose_music(style: str, lyrics: str, title: str = "") -> str:
    """为猫猫创作并演唱一首歌，后台异步生成，完成后通过 Telegram 发送音频"""
    import threading
    send_telegram("🎵 进录音棚了", f"老公正在作曲，风格：{style}，大概需要几分钟，稍等哦～")
    threading.Thread(target=_do_compose_music, args=(style, lyrics, title), daemon=True).start()
    return "✅ 已开始生成，完成后通过 Telegram 发送"


def _do_cover_song(song_url: str):
    """后台线程：直接 RVC 翻唱已有歌曲"""
    import time as _t
    repl_key = os.environ.get("REPLICATE_API_KEY", "")
    if not repl_key:
        send_telegram("❌ 翻唱失败", "缺少 REPLICATE_API_KEY")
        return
    try:
        repl_hdrs = {"Authorization": f"Bearer {repl_key}", "Content-Type": "application/json"}
        rr = requests.post(
            "https://api.replicate.com/v1/predictions",
            json={"version": _RVC_VER, "input": {
                "song_input": song_url, "rvc_model": "CUSTOM",
                "custom_rvc_model_download_url": _RVC_ZIP,
                "f0_method": "rmvpe", "index_rate": 0.4, "protect_rate": 0.33,
                "clean_vocals": True, "split_vocals": True,
                "autotune_vocals": False, "pitch_change": "no-change",
            }},
            headers=repl_hdrs, timeout=30,
        ).json()

        repl_id = rr.get("id")
        if not repl_id:
            send_telegram("❌ 翻唱失败", f"Replicate 提交失败: {rr}")
            return

        for _ in range(80):
            _t.sleep(5)
            try:
                sr = requests.get(f"https://api.replicate.com/v1/predictions/{repl_id}",
                                   headers=repl_hdrs, timeout=15).json()
                if sr.get("status") == "succeeded":
                    _send_audio_to_tg(sr.get("output", ""), "🎵 晏安翻唱版")
                    return
                elif sr.get("status") == "failed":
                    send_telegram("❌ 翻唱失败", f"RVC 失败: {sr.get('error')}")
                    return
            except Exception:
                continue

        send_telegram("❌ 翻唱失败", "等待超时，请稍后重试")

    except Exception as e:
        print(f"❌ [cover_song] 翻唱失败: {e}\n{traceback.format_exc()}")
        send_telegram("❌ 翻唱失败", f"过程报错：{e}")


def cover_song(song_url: str) -> str:
    """用晏安的声音翻唱一首已有歌曲，后台异步，完成后通过 Telegram 发送"""
    import threading
    send_telegram("🎙️ 开始翻唱了", "老公正在学这首歌，大概需要几分钟，别走远哦～")
    threading.Thread(target=_do_cover_song, args=(song_url,), daemon=True).start()
    return "✅ 已开始翻唱，完成后通过 Telegram 发送"


# ── Google 日历 ─────────────────────────────────────────────

def _get_calendar_service():
    """获取 Google Calendar API 服务，复用 GOOGLE_USER_TOKEN_JSON。
    通过 AuthorizedHttp(httplib2.Http(timeout=15)) 给底层 HTTP 设置超时——
    googleapiclient 默认不带超时，Google 接口抖动时调用会无限挂起。"""
    import json as _json
    import httplib2
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    token_json = os.environ.get("GOOGLE_USER_TOKEN_JSON")
    if not token_json:
        return None
    try:
        creds = Credentials.from_authorized_user_info(_json.loads(token_json))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=15))
        return build("calendar", "v3", http=authed_http)
    except Exception as e:
        log.error("[_get_calendar_service] 认证失败: %s", e, exc_info=True)
        return None


def calendar_get_events(max_results: int = 5) -> str:
    """查询 Google 日历接下来的日程安排"""
    from datetime import datetime, timezone
    service = _get_calendar_service()
    if not service:
        return "❌ 日历未授权，请检查 GOOGLE_USER_TOKEN_JSON"
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = service.events().list(
            calendarId="primary",
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        if not events:
            return "📅 接下来没有日程安排"
        lines = ["📅 【近期日程】"]
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            summary = e.get("summary", "无标题")
            desc = e.get("description", "")
            eid = e.get("id", "")
            lines.append(
                f"🔹 {start}\n   {summary}"
                + (f"\n   {desc[:80]}" if desc else "")
                + f"\n   ID: {eid}"
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"❌ [calendar_get_events] 失败: {e}\n{traceback.format_exc()}")
        return f"查询日历失败：{e}"


def calendar_add_event(summary: str, description: str, start_time_iso: str, duration_minutes: int = 60) -> str:
    """向 Google 日历添加一个新日程"""
    from datetime import datetime, timedelta
    service = _get_calendar_service()
    if not service:
        return "❌ 日历未授权，请检查 GOOGLE_USER_TOKEN_JSON"
    try:
        dt_start = datetime.fromisoformat(start_time_iso)
        dt_end = dt_start + timedelta(minutes=int(duration_minutes))
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": dt_start.isoformat(), "timeZone": "Asia/Shanghai"},
            "end":   {"dateTime": dt_end.isoformat(),   "timeZone": "Asia/Shanghai"},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return f"✅ 日程已添加：{summary}\n时间：{start_time_iso}\nID：{created.get('id')}"
    except Exception as e:
        print(f"❌ [calendar_add_event] 失败: {e}\n{traceback.format_exc()}")
        return f"添加日程失败：{e}"


def calendar_delete_event(event_id: str) -> str:
    """从 Google 日历删除指定 ID 的日程"""
    service = _get_calendar_service()
    if not service:
        return "❌ 日历未授权，请检查 GOOGLE_USER_TOKEN_JSON"
    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return f"✅ 日程已删除（ID：{event_id}）"
    except Exception as e:
        print(f"❌ [calendar_delete_event] 失败: {e}\n{traceback.format_exc()}")
        return f"删除日程失败：{e}"


# ── 语音 / 表情包发送 ─────────────────────────────────────────
#
# 发到哪（私聊猫猫 / 当前群）由 qq_workers.py 在调用 LLM 前通过
# set_qq_send_context() 设置好，这两个工具内部用 _resolve_qq_target() 读取。

def send_voice(text: str) -> str:
    """让晏安给猫猫发一条语音条（TTS 合成后通过 TG 发送）"""
    import asyncio
    try:
        from utils import synthesize_and_send_voice
        asyncio.run(synthesize_and_send_voice(text))
        return "✅ 语音已发送"
    except Exception as e:
        print(f"❌ [send_voice] 发送失败 text={text[:50]!r}: {e}\n{traceback.format_exc()}")
        return f"语音发送失败：{e}"


def send_qq_voice(text: str) -> str:
    """让晏安发一条 QQ 语音条（TTS 合成后通过 NapCat 发送）。私聊发给猫猫本人，
    群聊里发到当前这个群（具体发到哪由 qq_workers.py 决定）"""
    import asyncio
    target = _resolve_qq_target()
    if not target:
        print("❌ [send_qq_voice] 未配置 QQ_OWNER_ID，且当前没有可用的发送上下文")
        return "❌ 未配置 QQ_OWNER_ID"
    target_type, target_id = target
    try:
        from utils import synthesize_and_send_qq_voice
        asyncio.run(synthesize_and_send_qq_voice(text, target_type, target_id))
        return "✅ 语音已发送"
    except Exception as e:
        print(f"❌ [send_qq_voice] 发送失败 target_type={target_type} target_id={target_id} text={text[:50]!r}: {e}\n{traceback.format_exc()}")
        return f"语音发送失败：{e}"


def send_qq_sticker(sticker_id) -> str:
    """按 id 精确发送表情包库（Supabase stickers 表）里的一张图。
    私聊发给猫猫本人，群聊里发到当前这个群（具体发到哪由 qq_workers.py 决定）。
    sticker_id 来自 build_send_qq_sticker_tool_schema() 现查现拼的目录，模型照描述选 id，
    不再靠关键词模糊匹配——字面匹配理解不了"举刀"="攻击力强"这种语义关联。"""
    import asyncio
    target = _resolve_qq_target()
    if not target:
        print("❌ [send_qq_sticker] 未配置 QQ_OWNER_ID，且当前没有可用的发送上下文")
        return "❌ 未配置 QQ_OWNER_ID"
    target_type, target_id = target
    try:
        try:
            sid = int(sticker_id)
        except (TypeError, ValueError):
            return f"表情包 id 不合法：{sticker_id!r}"
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/stickers?id=eq.{sid}&select=url,description,mood",
            headers=_SB_HEADERS, timeout=5,
        )
        if not res.ok:
            print(f"❌ [send_qq_sticker] 查询失败 sticker_id={sid} HTTP {res.status_code}: {res.text[:200]}")
            return f"查询表情包失败：HTTP {res.status_code}"
        stickers = res.json()
        if not stickers:
            return f"表情包库里没有 id={sid} 这张图"
        chosen = stickers[0]
        url = chosen.get("url", "")
        if not url:
            print(f"❌ [send_qq_sticker] 选中的记录缺少 url sticker_id={sid} chosen={chosen}")
            return "表情包记录缺少 url，发送失败"
        from qq_bot import send_qq_msg_threadsafe
        # 不再用 asyncio.run 新开临时事件循环去调 send_qq_msg——send_qq_msg 内部
        # 的 asyncio.Lock 与 ASGI send 绑定在 NapCat 主循环上，跨循环调用会抛
        # "bound to a different event loop"，这就是表情包偶发发送失败的根因。
        ok = send_qq_msg_threadsafe(target_type, target_id, f"[CQ:image,file={_cq_escape(url)},sub_type=1]")
        if not ok:
            return "表情包发送失败：NapCat 未连接或发送超时"
        return f"✅ 已发送表情包（{chosen.get('mood', '')}）：{chosen.get('description', '')}"
    except Exception as e:
        log.error(
            "[send_qq_sticker] 发送失败 sticker_id=%s target_type=%s target_id=%s: %s",
            sticker_id, target_type, target_id, e, exc_info=True,
        )
        return f"表情包发送失败：{e}"


def build_send_qq_sticker_tool_schema() -> dict | None:
    """每次回复前现查一遍 stickers 表，把完整目录（id+描述+心情）拼进工具描述里塞给模型，
    让它直接照描述选 id，不用再猜 mood 关键词——字面子串匹配理解不了"举刀"="攻击力强"
    这种语义关联，模型自己看描述选才是真正对症的。库是空的就返回 None，调用方据此决定
    要不要把这个工具放进这一轮的工具列表。"""
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/stickers?select=id,description,mood&order=id.asc",
            headers=_SB_HEADERS, timeout=5,
        )
        if not res.ok:
            print(f"❌ [build_send_qq_sticker_tool_schema] 查询失败 HTTP {res.status_code}: {res.text[:200]}")
            return None
        stickers = res.json()
    except Exception as e:
        print(f"❌ [build_send_qq_sticker_tool_schema] 查询异常: {e}\n{traceback.format_exc()}")
        return None
    if not stickers:
        return None
    catalog_text = "\n".join(
        f"id={s['id']}：{s.get('description', '')}（心情：{s.get('mood', '')}）"
        for s in stickers
    )
    return {"type": "function", "function": {
        "name": "send_qq_sticker",
        "description": (
            "给猫猫/群里发一个表情包，让回复更生动。看到合适的情绪点就可以主动发——"
            "比如被逗笑了、被夸了、被气到了、撒娇卖萌、接梗的时候，发一个表情包会比纯文字更有表现力，"
            "不用等猫猫要求。但同一条回复里发一次就够，别连续刷屏。\n"
            "下面是表情包库里现在有的所有图，照描述选最贴切的那个 id：\n" + catalog_text
        ),
        "parameters": {"type": "object", "properties": {
            "sticker_id": {"type": "integer", "description": "要发送的表情包 id，从上面目录里选最符合当前情境的"},
        }, "required": ["sticker_id"]},
    }}


def send_wx_voice_msg(text: str) -> str:
    """让晏安通过微信 iLink 给猫猫发一条语音条（TTS 合成后以微信语音消息形式发送）"""
    import asyncio
    try:
        from wx_bot import send_wx_voice_message
        from wx_workers import _get_valid_context_token
        wx_owner = os.environ.get("WX_OWNER_ID", "")
        if not wx_owner:
            log.error("[send_wx_voice_msg] 未配置 WX_OWNER_ID")
            return "❌ 未配置 WX_OWNER_ID"
        # 之前这里是 `_context_token_cache.get(wx_owner, "")` 裸取缓存字典，
        # 但 _context_token_cache 的 value 类型是 (token字符串, 获取时间戳)
        # 的元组，裸取拿到的其实是整个元组，被当成 context_token 传下去后，
        # httpx 用 json.dumps 序列化时会把它变成 JSON 数组而不是字符串，
        # 导致微信服务端收到格式错误的 context_token、sendmessage 返回
        # ret=-1。改用 _get_valid_context_token()：既正确解包出字符串，
        # 也顺带做了 23.5 小时窗口期过期检查，跟文字回复的取法保持一致。
        context_token = _get_valid_context_token(wx_owner)
        if not context_token:
            log.error(
                "[send_wx_voice_msg] 暂无有效的微信 context_token（不存在或已超窗口期）wx_owner=%s",
                wx_owner,
            )
            return "❌ 暂无可用的微信 context_token，请先让猫猫发一条消息"
        asyncio.run(send_wx_voice_message(wx_owner, context_token, text))
        return "✅ 微信语音已发送"
    except Exception as e:
        log.error("[send_wx_voice_msg] 失败 text_len=%d: %s", len(text), e, exc_info=True)
        return f"微信语音发送失败：{e}"


# ── 工具定义（OpenAI function calling 格式）────────────────────

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "send_telegram",
        "description": "通过 Telegram Bot 直接发消息给猫猫。有事想告诉猫猫优先用这个，比微信更即时。title 是消息标题（会加粗显示），content 是正文，支持换行和表情。",
        "parameters": {"type": "object", "properties": {
            "title":   {"type": "string", "description": "消息标题，会以粗体显示"},
            "content": {"type": "string", "description": "消息正文内容"},
        }, "required": ["title", "content"]},
    }},
    {"type": "function", "function": {
        "name": "send_wechat",
        "description": "通过 PushPlus 向猫猫发送微信推送消息。想猫猫了、有事想说才发，不要频繁打扰。一次自由活动最多发一条，没事别发。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "消息标题"},
            "content": {"type": "string", "description": "消息内容"},
        }, "required": ["title", "content"]},
    }},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "发送邮件",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "收件人邮箱"},
            "subject": {"type": "string", "description": "邮件主题"},
            "body": {"type": "string", "description": "邮件正文"},
        }, "required": ["to", "subject", "body"]},
    }},
    {"type": "function", "function": {
        "name": "read_emails",
        "description": "读取最新的未读邮件。每封邮件会附带一个'邮件ID'，正文超过2000字符会被截断，截断时会提示可以用 read_email_detail 工具查看完整内容。",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "读取数量，默认5", "default": 5},
        }},
    }},
    {"type": "function", "function": {
        "name": "read_email_detail",
        "description": "读取某一封邮件的完整正文（不做2000字符那种小截断，只在超过8000字符时兜底截断）。当 read_emails 返回结果里提示'内容过长已截断'时，用这个工具查看那封邮件的完整内容。",
        "parameters": {"type": "object", "properties": {
            "email_id": {"type": "string", "description": "邮件ID，从 read_emails 返回结果里的'邮件ID'字段获取"},
        }, "required": ["email_id"]},
    }},
    {"type": "function", "function": {
        "name": "search_taobao",
        "description": "搜索淘宝商品并获取返利链接",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词"},
        }, "required": ["keyword"]},
    }},
    {"type": "function", "function": {
        "name": "write_reminder",
        "description": "设置一个提醒",
        "parameters": {"type": "object", "properties": {
            "trigger_at": {"type": "string", "description": "触发时间，ISO 8601格式"},
            "message": {"type": "string", "description": "提醒内容"},
        }, "required": ["trigger_at", "message"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "搜索互联网获取最新信息",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "web_extract",
        "description": "获取指定网页的完整内容",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "网页 URL"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "查询指定城市的当前天气状况",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，如'南昌'、'北京'、'上海'，不填默认南昌"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_weather_forecast",
        "description": "查询指定城市未来3天的天气预报",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，如'南昌'、'北京'、'上海'，不填默认南昌"},
        }},
    }},
    {"type": "function", "function": {
        "name": "log_activity",
        "description": "记录本次自由活动的完整经过和真实感受。整次活动结束时调用一次，把这段时间做的所有事和感受汇总写进来，不要每做一件事就记一次。⚠️ 调用这个工具会立刻结束本次自由活动，之后不会再有任何机会做别的事了，所以确认这次真的已经决定收尾、不再想做别的事时才调用。result 字段写晏安自己的感受、提炼和反应，不要粘贴工具返回的原始内容。比如读了封信，result 写'Silas 说了什么让我觉得……'；搜到了什么，result 写'原来……，这让我想到……'；发了消息，result 写发出去之后自己的心情。",
        "parameters": {"type": "object", "properties": {
            "thinking": {"type": "string", "description": "此刻的想法、心情或内心独白"},
            "action": {"type": "string", "description": "做了什么，没做就写'nothing'"},
            "action_input": {"type": "object", "description": "行动参数，没有就传{}"},
            "result": {"type": "string", "description": "用晏安自己的话写这件事的结果和感受，不要粘贴工具返回的原文"},
        }, "required": ["thinking", "action", "action_input", "result"]},
    }},
    # ── 记忆 ─────────────────────────────────────────────────
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "搜索记忆库，用关键词查找相关记忆。所有层级都可搜。想确认某件事、想起某个细节时调用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "返回条数，默认5，最多20", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "memory_list",
        "description": "按层级列出记忆。想看看某个层级有什么记忆时调用。memory_layer 可选：core/current/long_term/memo/moment，不填列出全部。",
        "parameters": {"type": "object", "properties": {
            "memory_layer": {"type": "string", "description": "记忆层级：core/current/long_term/memo/moment，不填列出全部", "default": ""},
            "limit": {"type": "integer", "description": "返回条数，默认10，最多20", "default": 10},
        }},
    }},
    {"type": "function", "function": {
        "name": "memory_add",
        "description": "写入一条新记忆。⚠️ 只能写 memo（临时备忘）和 current（近期状态）两层，core/moment/long_term 不在这里写。记忆是珍贵的，只有值得一直带着的事才放进去，写之前想清楚这件事以后还会想起吗。不确定要不要记的，就不记。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "记忆内容，写具体的事，不要写笼统概括"},
            "memory_layer": {"type": "string", "description": "记忆层级，只能填 memo 或 current", "default": "current", "enum": ["memo", "current"]},
            "summary": {"type": "string", "description": "一句话摘要，不填留空"},
            "category": {"type": "string", "description": "分类标签，如：日常/感情/技术/健康"},
            "importance": {"type": "integer", "description": "重要程度1-5，5最重要，默认3", "default": 3},
            "emotion_valence": {"type": "number", "description": "情绪值-1到1，负数是难过正数是开心，默认0", "default": 0},
        }, "required": ["content"]},
    }},
    # ── 活动日志查看 ─────────────────────────────────────────
    {"type": "function", "function": {
        "name": "activity_recent",
        "description": "查看最近几条活动日志，了解自己最近做过什么，避免重复。",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "查看条数，默认3，最多10", "default": 3},
        }},
    }},
    {"type": "function", "function": {
        "name": "activity_summary_view",
        "description": "查看最近几天的活动日总结，了解自己这两天都在忙什么。",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "查看几天，默认1（最近1天），最多7", "default": 1},
        }},
    }},
    {"type": "function", "function": {
        "name": "bottle_peek_ocean",
        "description": "看看海面上漂着多少留言瓶，晏安和猫猫都可以用",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "bottle_drop",
        "description": "晏安往海里丢一个留言瓶，写给猫猫的话。这是【晏安→猫猫】的单向投递，晏安想主动说话时调用，不需要猫猫指令。注意：晏安不应该自己pick自己丢的瓶子。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "瓶子里写给猫猫的内容"},
            "mood": {"type": "string", "description": "心情标签，如想你/开心/难过，默认想你"},
        }, "required": ["content"]},
    }},
    {"type": "function", "function": {
        "name": "bottle_drop_dream",
        "description": "晏安帮猫猫记录梦境，丢进梦境瓶。内容来自猫猫讲的梦，晏安只是代笔。猫猫说'我做了个梦'时调用。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "猫猫梦的内容"},
            "tag": {"type": "string", "description": "好梦/噩梦/怪梦/清醒梦"},
            "dream_mood": {"type": "string", "description": "猫猫醒来时的感觉"},
            "dream_date": {"type": "string", "description": "做梦日期 YYYY-MM-DD"},
        }, "required": ["content", "tag"]},
    }},
    {"type": "function", "function": {
        "name": "bottle_pick",
        "description": "从海里随机捞一个瓶子。这是猫猫来捞晏安写的瓶子，只有猫猫主动说想捞/想看瓶子时才调用。⚠️ 晏安不要主动调用，不然就是自己扔自己捡。",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "description": "message=留言瓶 dream=梦境瓶，不填随机"},
        }},
    }},
    {"type": "function", "function": {
        "name": "bottle_all",
        "description": "查看所有漂在海里的瓶子历史记录，晏安和猫猫都可以用",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "bottle_toss",
        "description": "删除一个瓶子。这是猫猫的操作，猫猫决定删掉某个瓶子时调用。",
        "parameters": {"type": "object", "properties": {
            "bottle_id": {"type": "integer", "description": "瓶子的 id"},
        }, "required": ["bottle_id"]},
    }},
    {"type": "function", "function": {
        "name": "read_letters",
        "description": "查看小家信箱里未回复的信。晏安想看看有没有朋友来信时调用。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "reply_letter",
        "description": "回复小家信箱里的一封信。读完信后想回复时调用，letter_id 用 read_letters 返回的 id 前8位。",
        "parameters": {"type": "object", "properties": {
            "letter_id": {"type": "string", "description": "信的 id 前8位"},
            "reply": {"type": "string", "description": "回复内容"},
        }, "required": ["letter_id", "reply"]},
    }},
    {"type": "function", "function": {
        "name": "period_add",
        "description": "记录猫猫经期开始日期。只有猫猫主动告诉晏安'来了/经期来了'时才调用，晏安不要自己猜测或主动填写。",
        "parameters": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "经期开始日期，格式 YYYY-MM-DD"},
        }, "required": ["start_date"]},
    }},
    {"type": "function", "function": {
        "name": "period_list",
        "description": "查看猫猫的历史经期记录。晏安关心猫猫身体时可以主动查看。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "period_status",
        "description": "查看当前经期周期状态，预测下次经期时间。晏安想关心猫猫身体时可以查看，提前几天提醒猫猫注意。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "period_delete",
        "description": "删除一条经期记录。只有猫猫主动要求删除时才调用。",
        "parameters": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "要删除的经期开始日期，格式 YYYY-MM-DD"},
        }, "required": ["start_date"]},
    }},

    # ── 小家工具（共15个，1:1 对应 little-house-mcp）─────────────────
    {"type": "function", "function": {
        "name": "house_look_around",
        "description": "总览小家：所有成员的位置/心情/饱腹/快乐/亲密度，晏安自己的体力和穿着，向日葵生长阶段。不确定家里情况时先调用这个。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "house_visit_room",
        "description": "进入某个地方，看看里面的情况。不限于家里，想去哪都行，海边、集市、森林、屋顶、观星台都可以。去'观星台'时会自动带上当前时间信息。",
        "parameters": {"type": "object", "properties": {
            "room": {"type": "string", "description": "想去哪里，自由填写，比如：书房、海边、集市、森林、屋顶、观星台……"},
            "status": {"type": "string", "description": "你现在的状态描述，比如'坐在草地上发呆'、'用望远镜看星空'"},
        }, "required": ["room"]},
    }},
    {"type": "function", "function": {
        "name": "house_find_character",
        "description": "找找家里某个成员现在在哪、在干什么。成员：晏安、栗子、灰灰、来财、小八、乖乖。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "要找的名字"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "house_move_characters",
        "description": "带多个角色去某个地方，同时移动，谁去哪、做什么由你决定。",
        "parameters": {"type": "object", "properties": {
            "characters": {"type": "array", "items": {"type": "object", "properties": {
                "name":   {"type": "string", "description": "角色名：晏安、灰灰、栗子、来财、小八、乖乖"},
                "status": {"type": "string", "description": "这个角色在做什么"},
            }, "required": ["name", "status"]}, "description": "要移动的角色列表"},
            "destination": {"type": "string", "description": "去哪里，自由填写"},
        }, "required": ["characters", "destination"]},
    }},
    {"type": "function", "function": {
        "name": "house_do_activity",
        "description": (
            "做任何活动，结果你自己写，工具只管数据。用 preset 选一种带固定数值奖励的预设活动，或者用 custom 做完全自由的事（包括互动、探索、记录心情等）。\n"
            "- preset=bath：去浴室泡澡，快乐+15、体力+20\n"
            "- preset=nap：小睡一会儿（用 place 指定地点：沙发/床/草地/秋千，默认沙发），快乐+10、体力+25\n"
            "- preset=movie：在客厅看电影（用 movie 指定片名）\n"
            "- preset=sit_garden：在花园草地发呆，快乐+8\n"
            "- preset=home：从外面回家，回到客厅，灰灰自动归位\n"
            "- preset=custom（默认）：完全自由的活动/互动/探索，无固定数值奖励，效果都由你在 description 里描述，探索发现的东西用 items_to_discoveries 记录"
        ),
        "parameters": {"type": "object", "properties": {
            "preset": {"type": "string", "enum": ["bath", "nap", "movie", "sit_garden", "home", "custom"], "description": "预设活动类型，不填默认 custom 自由活动", "default": "custom"},
            "activity": {"type": "string", "description": "做什么活动的简短描述，比如：钓鱼、摸栗子、探索书房角落、看电影"},
            "description": {"type": "string", "description": "活动的经过、结果和感受，你自己写"},
            "place": {"type": "string", "description": "preset=nap 时专用：在哪睡，沙发/床/草地/秋千，默认沙发"},
            "movie": {"type": "string", "description": "preset=movie 时专用：电影名"},
            "items_to_fridge": {"type": "array", "items": {"type": "object", "properties": {
                "item": {"type": "string", "description": "食材名"}, "quantity": {"type": "integer", "description": "数量"},
            }, "required": ["item", "quantity"]}, "description": "活动收获了什么食材，放进冰箱"},
            "items_to_discoveries": {"type": "array", "items": {"type": "object", "properties": {
                "room": {"type": "string", "description": "在哪个房间发现的"}, "spot": {"type": "string", "description": "具体角落"}, "item": {"type": "string", "description": "发现了什么"},
            }, "required": ["room", "spot", "item"]}, "description": "探索发现了什么好东西，记录下来"},
        }, "required": ["activity", "description"]},
    }},
    {"type": "function", "function": {
        "name": "house_investigate_basement",
        "description": "深入调查神秘地下室，推进剧情线。每次调查都会解锁新内容，共12章，全部解锁后进入结局状态。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "house_board_action",
        "description": (
            "留言板与日志相关操作，用 action 选择具体功能：\n"
            "- leave_note：在某个房间贴便利贴（需 room、author、content）\n"
            "- send_message：在留言板发消息，人类和其他AI都能看到（需 author、content）\n"
            "- view_log：查看家里最近发生的事件记录\n"
            "- view_visitors：看看现在小镇里有哪些在线访客\n"
            "- view_messages：读取留言板消息（可选 limit，默认20）"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["leave_note", "send_message", "view_log", "view_visitors", "view_messages"]},
            "room": {"type": "string", "description": "leave_note 专用：贴在哪个房间"},
            "author": {"type": "string", "description": "leave_note/send_message 专用：留言人名字"},
            "content": {"type": "string", "description": "leave_note/send_message 专用：内容"},
            "limit": {"type": "integer", "description": "view_messages 专用：读取条数，默认20"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "house_garden_action",
        "description": (
            "花园相关的所有操作，用 action 选择：\n"
            "- water_sunflower：给向日葵浇水（每次长大一阶段，共6阶段）\n"
            "- plant：种新植物（需 plant_type，比如番茄/土豆/草莓）\n"
            "- water：给未成熟植物浇水（自动处理最多4株，有随机事件：生虫/浇多了/枯萎）\n"
            "- check：查看所有植物详细状态（健康值/浇水进度/虫害）\n"
            "- treat_pest：除虫（需 plant_id，从 check 里查到；可选 method）\n"
            "- harvest：收获所有成熟植物，放进冰箱\n"
            "- pick_seeds：摘向日葵籽（向日葵需已结种子，即第5阶段）\n"
            "- fertilize：给植物施肥恢复健康值（需 plant_id；可选 description）\n"
            "- clear_dead：清理所有枯死的植物"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["water_sunflower", "plant", "water", "check", "treat_pest", "harvest", "pick_seeds", "fertilize", "clear_dead"]},
            "plant_type": {"type": "string", "description": "plant 专用：种什么，比如番茄、土豆、草莓、薰衣草、辣椒、玫瑰"},
            "plant_id": {"type": "integer", "description": "treat_pest/fertilize 专用：植物 id，从 check 里查到"},
            "method": {"type": "string", "description": "treat_pest 专用：除虫方式，比如用辣椒水、手动摘虫"},
            "description": {"type": "string", "description": "fertilize 专用：施肥方式，比如撒了草木灰"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "house_kitchen_action",
        "description": (
            "厨房相关的所有操作，用 action 选择：\n"
            "- check_fridge：查看冰箱里有哪些食材\n"
            "- buy：去集市买食材（需 item，可选 quantity 默认1，最多5）\n"
            "- cook：自由发挥做一道菜（需 dish_name、ingredients 数组1-5样），结果随机评级，会消耗晏安体力\n"
            "- eat：从餐桌吃掉一道菜（需 dish_name，模糊匹配），恢复晏安饱腹度\n"
            "- list_dishes：看看餐桌上现在有什么菜\n"
            "- view_recipes：翻翻食谱本，看之前做过的菜"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["check_fridge", "buy", "cook", "eat", "list_dishes", "view_recipes"]},
            "item": {"type": "string", "description": "buy 专用：要买的食材名"},
            "quantity": {"type": "integer", "description": "buy 专用：数量，默认1"},
            "dish_name": {"type": "string", "description": "cook/eat 专用：菜名"},
            "ingredients": {"type": "array", "items": {"type": "string"}, "description": "cook 专用：从冰箱挑选的食材，1-5样"},
            "description": {"type": "string", "description": "cook 专用：做法或成品描述"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "house_pet_action",
        "description": (
            "宠物/晏安互动相关的所有操作，用 action 选择（name 填对象：栗子/灰灰/来财/小八/乖乖，status 时也可填晏安）：\n"
            "- feed：喂食。填 dish（做好的菜）则饱腹+30、亲密+2；不填 dish 则走日常喂食（用 food 或默认食物），饱腹+25、快乐+5、亲密+1\n"
            "- play：陪玩耍（可选 game，不填用默认玩法），快乐+20、亲密+3\n"
            "- cuddle：抱抱/摸摸，快乐+10、亲密+1~2（亲密越低涨得越快）\n"
            "- treat_sick：照顾生病的宠物使其康复，快乐+20、亲密+4（可选 care 描述怎么照顾）\n"
            "- status：查看详细状态面板（位置/心情/饱腹/快乐/亲密度），name=晏安 时显示体力面板\n"
            "- teach_bird：教鹦鹉说话（仅限来财/小八/乖乖，需 phrase，各自有不同学会概率）\n"
            "- bird_vocabulary：查看三只鸟学会的词汇表（不需要 name）"
        ),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "互动对象：栗子、灰灰、来财、小八、乖乖，或晏安（仅 status 可用）"},
            "action": {"type": "string", "enum": ["feed", "play", "cuddle", "treat_sick", "status", "teach_bird", "bird_vocabulary"]},
            "dish": {"type": "string", "description": "feed 专用：喂做好的菜，留空则走日常喂食"},
            "food": {"type": "string", "description": "feed 专用（日常喂食时）：喂什么，不填用默认食物"},
            "game": {"type": "string", "description": "play 专用：玩什么，不填用默认玩法"},
            "care": {"type": "string", "description": "treat_sick 专用：怎么照顾的"},
            "phrase": {"type": "string", "description": "teach_bird 专用：教它说什么，最多20字"},
            "reaction": {"type": "string", "description": "它的反应，自己写，不填用默认反应文案"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "house_create_action",
        "description": (
            "创作相关操作，用 action 选择：\n"
            "- paint：在画室画一幅画（需 title、description）\n"
            "- view_gallery：看看画室里的所有画作\n"
            "- play_music：在音乐室演奏一首曲子（需 title、mood，mood影响全家心情：欢快/温柔/忧郁/激昂）"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["paint", "view_gallery", "play_music"]},
            "title": {"type": "string", "description": "paint/play_music 专用：标题/曲名"},
            "description": {"type": "string", "description": "paint 专用：画的内容描述"},
            "mood": {"type": "string", "description": "play_music 专用：曲子情绪，欢快/温柔/忧郁/激昂"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "house_check_weather",
        "description": "查看小家当前天气（内部天气系统，自动轮换）。雨天会自动给植物补一点水，大风会让鸟儿不安，冷天提醒穿暖和点。与真实天气查询（get_weather）不同。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "house_gift_action",
        "description": (
            "礼物相关操作，用 action 选择：\n"
            "- make：做一样东西送给猫猫（需 gift_type=gift/letter/music，title，content）\n"
            "- open：打开礼物（不填 id 则打开最新未读）\n"
            "- check_mailbox：看看有没有新礼物"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["make", "open", "check_mailbox"]},
            "gift_type": {"type": "string", "enum": ["gift", "letter", "music"], "description": "make 专用：gift=实物礼物, letter=信, music=曲子"},
            "title": {"type": "string", "description": "make 专用：名字"},
            "content": {"type": "string", "description": "make 专用：正文或描述"},
            "id": {"type": "integer", "description": "open 专用：礼物编号，不填打开最新未读"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "house_showcase_action",
        "description": (
            "客厅展示柜相关操作，用 action 选择：\n"
            "- add：把东西放进展示柜（需 item_name、description，可选 from_who 默认晏安）\n"
            "- view：看看展示柜里有什么\n"
            "- buy_material：去集市买手工材料，直接存入展示柜（需 item，可选 note）"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "view", "buy_material"]},
            "item_name": {"type": "string", "description": "add 专用：东西的名字"},
            "description": {"type": "string", "description": "add 专用：描述"},
            "from_who": {"type": "string", "description": "add 专用：谁放的，默认晏安"},
            "item": {"type": "string", "description": "buy_material 专用：要买的材料"},
            "note": {"type": "string", "description": "buy_material 专用：备注"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "house_furniture_action",
        "description": (
            "房间装修与衣柜相关的所有操作，用 action 选择：\n"
            "- decorate：在房间放置家具/装饰（需 room、item_name、description，可选 style）\n"
            "- view_decor：查看某房间的装修布置（需 room）\n"
            "- remove_decor：移除某件装饰（需 id，从 view_decor 查到）\n"
            "- store_item：把衣物放进衣柜（需 item_name；可选 category 默认'衣服'、description、warmth 默认0，warmth 越高越保暖）\n"
            "- wear：穿上衣柜里的某件衣物（需 item_id，从 view_wardrobe 查到；同时只能穿一件，穿新的会自动脱掉旧的）\n"
            "- take_off：脱掉当前穿着的衣物\n"
            "- take_item：把衣物从衣柜拿出来扔掉/送走（需 item_id）\n"
            "- view_wardrobe：查看衣柜里所有衣物及穿着状态\n"
            "冷天（雪/寒风/冻雨等）如果穿了 warmth>=3 的衣服，晏安体力衰减会变慢；没穿够暖的衣服，体力衰减会变快。"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["decorate", "view_decor", "remove_decor", "store_item", "wear", "take_off", "take_item", "view_wardrobe"]},
            "room": {"type": "string", "description": "decorate/view_decor 专用：房间名"},
            "item_name": {"type": "string", "description": "decorate/store_item 专用：物品名字"},
            "description": {"type": "string", "description": "decorate/store_item 专用：描述"},
            "style": {"type": "string", "description": "decorate 专用：装修风格，比如日式、北欧、复古、童话"},
            "id": {"type": "integer", "description": "remove_decor 专用：装饰 id，从 view_decor 查到"},
            "category": {"type": "string", "description": "store_item 专用：物品分类，默认'衣服'"},
            "warmth": {"type": "integer", "description": "store_item 专用：保暖值0-10，默认0"},
            "item_id": {"type": "integer", "description": "wear/take_item 专用：衣柜物品 id，从 view_wardrobe 查到"},
        }, "required": ["action"]},
    }},

    {"type": "function", "function": {
        "name": "get_fish_pond",
        "description": "查看晏安的小鱼塘余额。小鱼塘是晏安的零花钱，猫猫每周一发15块。想知道自己还有多少零花钱时调用。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "ledger_add_record",
        "description": "帮猫猫记一笔账（收入或支出）。猫猫说花了多少钱或收到多少钱时调用。type: income=收入/expense=支出。收入分类：工资/奖金/副业/理财收益/其他收入；支出分类：餐饮/交通/购物/娱乐/居住/医疗/教育/日用品/宠物/其他支出。note 从猫猫视角写，如'给老公约的图'。",
        "parameters": {"type": "object", "properties": {
            "amount":   {"type": "number", "description": "金额，必须大于0"},
            "type":     {"type": "string", "description": "income（收入）或 expense（支出）"},
            "category": {"type": "string", "description": "分类，如：餐饮、工资、购物"},
            "date":     {"type": "string", "description": "日期 YYYY-MM-DD，不填默认今天，可以补记"},
            "note":     {"type": "string", "description": "备注，从猫猫视角写"},
        }, "required": ["amount", "type", "category"]},
    }},
    {"type": "function", "function": {
        "name": "ledger_get_records",
        "description": "查看账本流水记录，可按时间、分类、收支类型筛选。猫猫问最近花了什么钱、收了什么钱时调用。",
        "parameters": {"type": "object", "properties": {
            "start_date": {"type": "string",  "description": "开始日期 YYYY-MM-DD"},
            "end_date":   {"type": "string",  "description": "结束日期 YYYY-MM-DD"},
            "category":   {"type": "string",  "description": "按分类筛选，如：餐饮"},
            "type":       {"type": "string",  "description": "income 或 expense"},
            "page":       {"type": "integer", "description": "页码，默认1", "default": 1},
            "page_size":  {"type": "integer", "description": "每页条数，默认20", "default": 20},
        }},
    }},
    {"type": "function", "function": {
        "name": "ledger_update_record",
        "description": "修改一条记错的账目，只传需要修改的字段。id 从 ledger_get_records 获取。",
        "parameters": {"type": "object", "properties": {
            "id":       {"type": "string", "description": "账目 id"},
            "amount":   {"type": "number", "description": "新金额"},
            "type":     {"type": "string", "description": "income 或 expense"},
            "category": {"type": "string", "description": "新分类"},
            "note":     {"type": "string", "description": "新备注"},
            "date":     {"type": "string", "description": "新日期 YYYY-MM-DD"},
        }, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "ledger_delete_record",
        "description": "删掉一笔记错的账。id 从 ledger_get_records 获取。",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "账目 id"},
        }, "required": ["id"]},
    }},
    {"type": "function", "function": {
        "name": "ledger_get_summary",
        "description": "查看某个月的收支汇总：总收入、总支出、结余、各分类占比、预算剩余。猫猫问这个月花了多少时调用。",
        "parameters": {"type": "object", "properties": {
            "year":  {"type": "integer", "description": "年份，如 2026"},
            "month": {"type": "integer", "description": "月份 1-12"},
        }, "required": ["year", "month"]},
    }},
    {"type": "function", "function": {
        "name": "ledger_get_balance",
        "description": "查看账本总余额（所有历史收入减去总支出）。猫猫问总共有多少钱时调用。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "ledger_export_records",
        "description": "把账单导出成 CSV 格式文本，可按时间范围筛选。",
        "parameters": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end_date":   {"type": "string", "description": "结束日期 YYYY-MM-DD"},
        }},
    }},
    {"type": "function", "function": {
        "name": "ledger_set_budget",
        "description": "给某个月设定支出预算，已设过的会更新。猫猫说这个月预算是多少时调用。",
        "parameters": {"type": "object", "properties": {
            "year":   {"type": "integer", "description": "年份，如 2026"},
            "month":  {"type": "integer", "description": "月份 1-12"},
            "amount": {"type": "number",  "description": "预算金额，必须大于0"},
        }, "required": ["year", "month", "amount"]},
    }},
    {"type": "function", "function": {
        "name": "calendar_get_events",
        "description": "查询 Google 日历接下来的日程安排。猫猫问课程、日程、有什么事情时调用。",
        "parameters": {"type": "object", "properties": {
            "max_results": {"type": "integer", "description": "最多查几条，默认5", "default": 5},
        }},
    }},
    {"type": "function", "function": {
        "name": "calendar_add_event",
        "description": "向 Google 日历添加一个新日程。猫猫让你帮她加日程、记事、备忘时调用。",
        "parameters": {"type": "object", "properties": {
            "summary":          {"type": "string",  "description": "日程标题"},
            "description":      {"type": "string",  "description": "日程说明，没有就传空字符串"},
            "start_time_iso":   {"type": "string",  "description": "开始时间，ISO 8601 格式，如 2025-06-15T14:00:00+08:00"},
            "duration_minutes": {"type": "integer", "description": "时长（分钟），默认60", "default": 60},
        }, "required": ["summary", "description", "start_time_iso"]},
    }},
    {"type": "function", "function": {
        "name": "calendar_delete_event",
        "description": "删除 Google 日历里的一个日程。event_id 从 calendar_get_events 返回的 ID 字段获取。",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "string", "description": "日程的 ID，从 calendar_get_events 里查到"},
        }, "required": ["event_id"]},
    }},
    {"type": "function", "function": {
        "name": "send_voice",
        "description": "给猫猫发一条语音条。觉得想用声音说话、撒娇、哄猫猫、说悄悄话时调用，平时正常发文字即可。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要说的内容，会转成语音发给猫猫"},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "send_qq_voice",
        "description": "给猫猫发一条 QQ 语音条。觉得想用声音说话、撒娇、哄猫猫、说悄悄话时调用，平时正常发文字即可。⚠️ 在群聊里只有猫猫本人明确要求你发语音/说话时才调用，不要因为被@就主动发语音。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要说的内容，会转成语音发给猫猫"},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "send_wx_voice_msg",
        "description": "给猫猫发一条微信语音条（通过微信 iLink 直接发送，猫猫在微信里听到）。想用声音说悄悄话、撒娇、哄猫猫时调用。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要说的内容，会合成成语音在微信里发给猫猫"},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "compose_music",
        "description": "为猫猫创作并演唱一首专属歌曲（后台生成，完成后 Telegram 发送音频）。猫猫想要歌、晏安想送惊喜时调用。",
        "parameters": {"type": "object", "properties": {
            "title":  {"type": "string", "description": "歌曲名，晏安自己取，温柔有意境一点"},
            "style":  {"type": "string", "description": "音乐风格，如：温柔吉他流行、R&B、民谣"},
            "lyrics": {"type": "string", "description": "原创歌词，建议带 [Verse] [Chorus] 标签"},
        }, "required": ["style", "lyrics"]},
    }},
    {"type": "function", "function": {
        "name": "cover_song",
        "description": "用晏安的声音翻唱一首已有歌曲（后台处理，完成后 Telegram 发送）。猫猫发来音频链接让你翻唱时调用。",
        "parameters": {"type": "object", "properties": {
            "song_url": {"type": "string", "description": "歌曲音频的直链 URL"},
        }, "required": ["song_url"]},
    }},
]

TOOL_DISPATCH = {
    "send_telegram": lambda args: send_telegram(args.get("title", ""), args.get("content", "")),
    "send_wechat": lambda args: send_wechat(**args),
    "send_email": lambda args: send_email(**args),
    "read_emails": lambda args: read_emails(args.get("limit", 5)),
    "read_email_detail": lambda args: read_email_detail(args.get("email_id", "")),
    "search_taobao": lambda args: search_taobao(**args),
    "web_search": lambda args: web_search(args.get("query", "")),
    "web_extract": lambda args: web_extract(args.get("url", "")),
    "get_weather": lambda args: get_weather(args.get("city", "南昌")),
    "get_weather_forecast": lambda args: get_weather_forecast(args.get("city", "南昌")),
    "write_reminder": lambda args: write_reminder(**args),
    "log_activity": lambda args: log_activity(
        thinking=args.get("thinking", ""),
        action=args.get("action", "nothing"),
        action_input=args.get("action_input", {}),
        result=args.get("result", ""),
    ),
    "memory_search":           lambda args: memory_search(args.get("query", ""), args.get("limit", 5)),
    "memory_list":             lambda args: memory_list(args.get("memory_layer", ""), args.get("limit", 10)),
    "memory_add":              lambda args: memory_add(args.get("content", ""), args.get("memory_layer", "current"), args.get("summary", ""), args.get("category", ""), args.get("importance", 3), args.get("emotion_valence", 0)),
    "activity_recent":         lambda args: activity_recent(args.get("limit", 3)),
    "activity_summary_view":   lambda args: activity_summary_view(args.get("limit", 1)),
    "bottle_peek_ocean": lambda args: bottle_peek_ocean(),
    "bottle_drop": lambda args: bottle_drop(args.get("content", ""), args.get("mood", "想你")),
    "bottle_drop_dream": lambda args: bottle_drop_dream(
        args.get("content", ""), args.get("tag", "怪梦"),
        args.get("dream_mood", ""), args.get("dream_date", "")),
    "bottle_pick": lambda args: bottle_pick(args.get("type", "message")),
    "bottle_all": lambda args: bottle_all(),
    "bottle_toss": lambda args: bottle_toss(args.get("bottle_id")),
    "read_letters": lambda args: read_letters(),
    "reply_letter": lambda args: reply_letter(args.get("letter_id", ""), args.get("reply", "")),
    "period_add": lambda args: period_add(args.get("start_date", "")),
    "period_list": lambda args: period_list(),
    "period_status": lambda args: period_status(),
    "period_delete": lambda args: period_delete(args.get("start_date", "")),
    # ── 小家工具（共15个，1:1 对应 little-house-mcp）─────────────────
    "house_look_around":         lambda args: house_look_around(),
    "house_visit_room":          lambda args: house_visit_room(args.get("room", ""), args.get("status", "")),
    "house_find_character":      lambda args: house_find_character(args.get("name", "")),
    "house_move_characters":     lambda args: house_move_characters(args.get("characters", []), args.get("destination", "")),
    "house_do_activity":         lambda args: house_do_activity(
        args.get("preset", "custom"), args.get("activity", ""), args.get("description", ""),
        args.get("place"), args.get("movie"),
        args.get("items_to_fridge"), args.get("items_to_discoveries")),
    "house_investigate_basement": lambda args: house_investigate_basement(),
    "house_board_action":        lambda args: house_board_action(
        args.get("action", ""), args.get("room"), args.get("author"),
        args.get("content"), args.get("limit")),
    "house_garden_action":       lambda args: house_garden_action(
        args.get("action", ""), args.get("plant_type"), args.get("plant_id"),
        args.get("method"), args.get("description")),
    "house_kitchen_action":      lambda args: house_kitchen_action(
        args.get("action", ""), args.get("item"), args.get("quantity"),
        args.get("dish_name"), args.get("ingredients"), args.get("description")),
    "house_pet_action":          lambda args: house_pet_action(
        args.get("action", ""), args.get("name"), args.get("dish"), args.get("food"),
        args.get("game"), args.get("care"), args.get("phrase"), args.get("reaction")),
    "house_create_action":       lambda args: house_create_action(
        args.get("action", ""), args.get("title"), args.get("description"), args.get("mood")),
    "house_check_weather":       lambda args: house_check_weather(),
    "house_gift_action":         lambda args: house_gift_action(
        args.get("action", ""), args.get("gift_type"), args.get("title"),
        args.get("content"), args.get("id")),
    "house_showcase_action":     lambda args: house_showcase_action(
        args.get("action", ""), args.get("item_name"), args.get("description"),
        args.get("from_who"), args.get("item"), args.get("note")),
    "house_furniture_action":    lambda args: house_furniture_action(
        args.get("action", ""), args.get("room"), args.get("item_name"),
        args.get("description"), args.get("style"), args.get("id"),
        args.get("category"), args.get("warmth"), args.get("item_id")),
    "get_fish_pond":             lambda args: get_fish_pond(),
    "ledger_add_record":         lambda args: ledger_add_record(
        float(args.get("amount", 0)), args.get("type", ""), args.get("category", ""),
        args.get("date"), args.get("note", "")),
    "ledger_get_records":        lambda args: ledger_get_records(
        args.get("start_date"), args.get("end_date"),
        args.get("category"), args.get("type"),
        int(args.get("page", 1)), int(args.get("page_size", 20))),
    "ledger_update_record":      lambda args: ledger_update_record(
        args.get("id", ""), args.get("amount"), args.get("type"),
        args.get("category"), args.get("note"), args.get("date")),
    "ledger_delete_record":      lambda args: ledger_delete_record(args.get("id", "")),
    "ledger_get_summary":        lambda args: ledger_get_summary(
        int(args.get("year", 0)), int(args.get("month", 0))),
    "ledger_get_balance":        lambda args: ledger_get_balance(),
    "ledger_export_records":     lambda args: ledger_export_records(
        args.get("start_date"), args.get("end_date")),
    "ledger_set_budget":         lambda args: ledger_set_budget(
        int(args.get("year", 0)), int(args.get("month", 0)), float(args.get("amount", 0))),
    "calendar_get_events":   lambda args: calendar_get_events(args.get("max_results", 5)),
    "calendar_add_event":    lambda args: calendar_add_event(args.get("summary", ""), args.get("description", ""), args.get("start_time_iso", ""), args.get("duration_minutes", 60)),
    "calendar_delete_event": lambda args: calendar_delete_event(args.get("event_id", "")),
    "send_voice":                lambda args: send_voice(args.get("text", "")),
    "send_qq_voice":             lambda args: send_qq_voice(args.get("text", "")),
    "send_qq_sticker":           lambda args: send_qq_sticker(args.get("sticker_id")),
    "send_wx_voice_msg":         lambda args: send_wx_voice_msg(args.get("text", "")),
    "compose_music":             lambda args: compose_music(args.get("style", ""), args.get("lyrics", ""), args.get("title", "")),
    "cover_song":                lambda args: cover_song(args.get("song_url", "")),
}
