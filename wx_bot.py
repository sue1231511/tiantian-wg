import os
import asyncio
import uuid
import time
import struct
import base64
import logging
import requests
import httpx

from bg_executor import track_task

log = logging.getLogger(__name__)

WX_ILINK_TOKEN   = os.environ.get("WX_ILINK_TOKEN", "")
WX_ILINK_BASEURL = os.environ.get("WX_ILINK_BASEURL", "https://ilinkai.weixin.qq.com").rstrip("/")
WX_ILINK_BOT_ID  = os.environ.get("WX_ILINK_BOT_ID", "")
PUSHPLUS_TOKEN   = os.environ.get("PUSHPLUS_TOKEN", "")
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")

_LONG_POLL_TIMEOUT = 28
_last_expire_notify: float = 0.0

# sendmessage 接口 ret=-2 = 限流，需要指数退避重试（区别于其他业务错误的立即重试）
_WX_RATE_LIMIT_MAX_RETRIES = 3      # 限流专用重试次数
_WX_RATE_LIMIT_BASE_DELAY  = 10     # 限流重试起始等待秒数
_WX_RATE_LIMIT_MAX_DELAY   = 60     # 限流重试等待秒数上限

# 语音发送链路（getuploadurl / CDN 上传 / sendmessage）的瞬时网络异常
# （连接超时/读超时/连接失败）专用重试，区别于限流重试和业务逻辑错误——
# 网络抖动值得重试，业务错误（如 ret 非0、响应格式错误）重试无意义，不纳入这里
_WX_NETWORK_MAX_RETRIES = 2         # 网络异常最大重试次数
_WX_NETWORK_RETRY_DELAY = 3         # 网络异常重试间隔秒数

# 运行时可被更新
_current_token = WX_ILINK_TOKEN
_current_bot_id = WX_ILINK_BOT_ID
_current_baseurl = WX_ILINK_BASEURL


def _random_wechat_uin() -> str:
    """X-WECHAT-UIN: 随机 uint32 → 十进制字符串 → base64"""
    uint32 = struct.unpack(">I", os.urandom(4))[0]
    return base64.b64encode(str(uint32).encode("utf-8")).decode("utf-8")


def _headers(token: str = "") -> dict:
    t = token or _current_token
    return {
        "Content-Type":           "application/json",
        "AuthorizationType":      "ilink_bot_token",
        "Authorization":          f"Bearer {t}",
        "X-WECHAT-UIN":           _random_wechat_uin(),
        "iLink-App-Id":           "bot",
        "iLink-App-ClientVersion": "65536",
    }


def _base_info() -> dict:
    return {"channel_version": "2.0.0"}


def _load_credentials_from_supabase() -> dict | None:
    """从 Supabase bot_settings 读取微信 token"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        keys = ["wx_ilink_token", "wx_ilink_bot_id", "wx_ilink_baseurl"]
        results = {}
        for key in keys:
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/bot_settings?key=eq.{key}&select=value",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                timeout=5,
            )
            data = resp.json()
            if data:
                results[key] = data[0].get("value", "")
        if results.get("wx_ilink_token"):
            return results
    except Exception as e:
        log.error("[_load_credentials_from_supabase] 从 Supabase 读取凭证失败: %s", e, exc_info=True)
    return None


def _notify_expired():
    global _last_expire_notify
    if not PUSHPLUS_TOKEN:
        return
    now = time.time()
    if now - _last_expire_notify < 3600:
        return
    _last_expire_notify = now
    try:
        requests.get(
            "http://www.pushplus.plus/send",
            params={
                "token": PUSHPLUS_TOKEN,
                "title": "⚠️ 微信 Bot token 已过期",
                "content": "微信 iLink session 已过期，请重新运行 wx_login.py 扫码。",
                "template": "html",
            },
            timeout=8,
        )
        log.info("[_notify_expired] 已发送微信 token 过期通知")
    except Exception as e:
        log.error("[_notify_expired] 推送通知失败: %s", e, exc_info=True)


async def send_wx_message(to_user_id: str, context_token: str, text: str, retries: int = 1) -> bool:
    if not _current_token or not _current_bot_id:
        log.warning("[send_wx_message] 未配置 token 或 bot_id，跳过发送 to_user_id=%s", to_user_id)
        return False
    if not context_token:
        log.error("[send_wx_message] context_token 为空，无法发送消息 to_user_id=%s", to_user_id)
        return False

    url = f"{_current_baseurl}/ilink/bot/sendmessage"
    payload = {
        "msg": {
            "from_user_id":  _current_bot_id,
            "to_user_id":    to_user_id,
            "client_id":     str(uuid.uuid4()),
            "message_type":  2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": _base_info(),
    }

    other_attempt = 0          # 普通业务错误/网络异常的重试计数（沿用 retries 参数）
    rate_limit_attempt = 0     # 限流（ret=-2）专用重试计数，不占用 retries 名额
    rate_limit_delay = _WX_RATE_LIMIT_BASE_DELAY

    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=_headers(), json=payload)
                data = resp.json()

            if data.get("errcode") == -14:
                log.error("[send_wx_message] sendmessage session 过期，请重新扫码 to_user_id=%s", to_user_id)
                return False

            ret = data.get("ret", 0)

            if ret == 0:
                log.info("[send_wx_message] → %s: %s", to_user_id, text[:40])
                return True

            if ret == -2:
                if rate_limit_attempt < _WX_RATE_LIMIT_MAX_RETRIES:
                    rate_limit_attempt += 1
                    log.warning(
                        "[send_wx_message] sendmessage 被限流（ret=-2），%ss 后第%d次重试 to_user_id=%s",
                        rate_limit_delay, rate_limit_attempt, to_user_id,
                    )
                    await asyncio.sleep(rate_limit_delay)
                    rate_limit_delay = min(rate_limit_delay * 2, _WX_RATE_LIMIT_MAX_DELAY)
                    continue
                log.error(
                    "[send_wx_message] sendmessage 限流重试%d次后仍失败 to_user_id=%s resp=%r",
                    _WX_RATE_LIMIT_MAX_RETRIES, to_user_id, data,
                )
                return False

            if other_attempt < retries:
                other_attempt += 1
                log.warning(
                    "[send_wx_message] sendmessage 第%d次失败，重试中 to_user_id=%s resp=%r",
                    other_attempt, to_user_id, data,
                )
                continue
            log.error(
                "[send_wx_message] sendmessage 失败 to_user_id=%s http_status=%s ret=%s errcode=%s "
                "context_token_type=%s context_token_len=%s resp=%r",
                to_user_id, resp.status_code, data.get("ret"), data.get("errcode"),
                type(context_token).__name__,
                len(context_token) if isinstance(context_token, str) else "N/A",
                data,
            )
            return False

        except Exception as e:
            if other_attempt < retries:
                other_attempt += 1
                log.warning(
                    "[send_wx_message] sendmessage 第%d次异常，重试中 to_user_id=%s: %s",
                    other_attempt, to_user_id, e, exc_info=True,
                )
                continue
            log.error(
                "[send_wx_message] sendmessage 异常 to_user_id=%s: %s",
                to_user_id, e, exc_info=True,
            )
            return False


async def async_wx_bot():
    global _current_token, _current_bot_id, _current_baseurl

    # 优先从 Supabase 读凭证（持久化存储）
    creds = await asyncio.to_thread(_load_credentials_from_supabase)
    if creds:
        _current_token   = creds.get("wx_ilink_token", "") or WX_ILINK_TOKEN
        _current_bot_id  = creds.get("wx_ilink_bot_id", "") or WX_ILINK_BOT_ID
        # baseurl 固定用环境变量（CF Worker 地址），不从 Supabase 读
        _current_baseurl = WX_ILINK_BASEURL
        log.info("[async_wx_bot] 从 Supabase 加载凭证 bot_id=%s...", _current_bot_id[:20])
    else:
        log.info("[async_wx_bot] 使用环境变量凭证")

    if not _current_token:
        log.warning("[async_wx_bot] 未配置 WX_ILINK_TOKEN，跳过微信 Bot 启动")
        return

    from wx_workers import handle_wx_message, _restore_context_token_cache
    await asyncio.to_thread(_restore_context_token_cache)
    log.info("[async_wx_bot] 微信 iLink Bot 已启动")

    url             = f"{_current_baseurl}/ilink/bot/getupdates"
    get_updates_buf = ""
    consecutive_err = 0

    while True:
        try:
            payload = {"get_updates_buf": get_updates_buf, "base_info": _base_info()}
            async with httpx.AsyncClient(timeout=_LONG_POLL_TIMEOUT) as client:
                resp = await client.post(url, headers=_headers(), json=payload)

            data = resp.json()

            # session 过期，尝试从 Supabase 读新 token
            if data.get("errcode") == -14:
                log.error("[async_wx_bot] session 过期（errcode=-14），请重新运行 wx_login.py 扫码")
                _notify_expired()
                # 尝试从 Supabase 读新凭证
                new_creds = await asyncio.to_thread(_load_credentials_from_supabase)
                if new_creds and new_creds.get("wx_ilink_token") != _current_token:
                    _current_token   = new_creds["wx_ilink_token"]
                    _current_bot_id  = new_creds.get("wx_ilink_bot_id", _current_bot_id)
                    _current_baseurl = (new_creds.get("wx_ilink_baseurl", _current_baseurl) or _current_baseurl).rstrip("/")
                    url = f"{_current_baseurl}/ilink/bot/getupdates"
                    log.info("[async_wx_bot] 检测到新 token，自动切换")
                    continue
                await asyncio.sleep(60)
                continue

            if data.get("ret", 0) != 0:
                consecutive_err += 1
                log.error("[async_wx_bot] getupdates 错误 连续错误=%d resp=%r", consecutive_err, data)
                await asyncio.sleep(min(5 * consecutive_err, 60))
                continue

            consecutive_err = 0
            new_buf = data.get("get_updates_buf", "")
            if new_buf:
                get_updates_buf = new_buf

            for msg in data.get("msgs") or []:
                track_task(asyncio.create_task(handle_wx_message(msg)))

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadTimeout):
            consecutive_err = 0
            await asyncio.sleep(0.5)

        except Exception as e:
            consecutive_err += 1
            wait = min(5 * consecutive_err, 60)
            log.error(
                "[async_wx_bot] 轮询异常 连续错误=%d，%ss 后重试: %s",
                consecutive_err, wait, e, exc_info=True,
            )
            await asyncio.sleep(wait)


# ── 微信语音发送 ──────────────────────────────────────────────

async def _wx_tts_to_mp3(text: str) -> bytes | None:
    """TTS 合成，返回 MP3 字节数据（优先 Minimax，回退 OpenAI TTS）"""
    minimax_key = os.environ.get("MINIMAX_API_KEY", "")
    voice_key   = os.environ.get("VOICE_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

    def _do() -> bytes | None:
        if minimax_key:
            url_tts = "https://api.minimax.chat/v1/t2a_v2"
            headers = {"Authorization": f"Bearer {minimax_key}", "Content-Type": "application/json"}
            payload = {
                "model": "speech-01-turbo",
                "text":  text[:1200],
                "stream": False,
                "voice_setting": {
                    "voice_id": os.environ.get(
                        "MINIMAX_VOICE_ID", "moss_audio_fd2620f9-bef3-11f0-8647-a697af11f3d9"
                    ),
                    "speed": 1.0, "vol": 1.0, "pitch": 0,
                },
                "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
            }
            try:
                resp = requests.post(url_tts, json=payload, headers=headers, timeout=30)
                if resp.status_code == 404:
                    resp = requests.post(
                        url_tts.replace("minimax.chat", "minimax.io"),
                        json=payload, headers=headers, timeout=30,
                    )
                res_json = resp.json()
                if res_json.get("base_resp", {}).get("status_code") == 0:
                    audio_hex = res_json.get("data", {}).get("audio", "")
                    if audio_hex:
                        return bytes.fromhex(audio_hex)
                log.error("[_wx_tts_to_mp3] Minimax 报错 text_len=%d resp=%r", len(text), res_json)
                return None
            except Exception as e:
                log.error("[_wx_tts_to_mp3] Minimax 异常 text_len=%d: %s", len(text), e, exc_info=True)
                return None
        if voice_key:
            try:
                from openai import OpenAI
                client = OpenAI(
                    api_key=voice_key,
                    base_url=os.environ.get("VOICE_BASE_URL", "https://api.openai.com/v1"),
                )
                res = client.audio.speech.create(model="tts-1", voice="echo", input=text[:1200])
                return res.content
            except Exception as e:
                log.error("[_wx_tts_to_mp3] OpenAI TTS 异常 text_len=%d: %s", len(text), e, exc_info=True)
                return None
        log.warning("[_wx_tts_to_mp3] 无 TTS API Key，跳过")
        return None

    return await asyncio.to_thread(_do)


def _convert_mp3_to_silk(mp3_data: bytes) -> tuple[bytes, int]:
    """MP3 → SILK（微信原生格式），返回 (silk_bytes, playtime_ms)"""
    import time as _t, subprocess as _sp
    ts        = int(_t.time())
    temp_mp3  = f"/tmp/wx_voice_{ts}.mp3"
    temp_pcm  = f"/tmp/wx_voice_{ts}.pcm"
    temp_silk = f"/tmp/wx_voice_{ts}.silk"

    with open(temp_mp3, "wb") as f:
        f.write(mp3_data)

    try:
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            ffmpeg_exe = "ffmpeg"

        # 诊断：打印 ffmpeg 实际版本，排查依赖库版本漂移（requirements.txt 未锁版本号）
        # 导致代码没改、但每次重新部署拉到的依赖行为变了的可能性
        try:
            ver_result = _sp.run([ffmpeg_exe, "-version"], capture_output=True, timeout=10, text=True)
            ffmpeg_version_line = (ver_result.stdout or "").splitlines()[0] if ver_result.stdout else "未知"
            log.info("[_convert_mp3_to_silk] ffmpeg 版本诊断 exe=%s version=%s", ffmpeg_exe, ffmpeg_version_line)
        except Exception as ver_err:
            log.warning("[_convert_mp3_to_silk] 获取 ffmpeg 版本失败（不影响转码流程）: %s", ver_err, exc_info=True)

        # MP3 → PCM（s16le，24000Hz，单声道——微信语音标准采样率，依据官方协议示例）
        r = _sp.run(
            [ffmpeg_exe, "-y", "-i", temp_mp3, "-ar", "24000", "-ac", "1", "-f", "s16le", temp_pcm],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            log.error(
                "[_convert_mp3_to_silk] ffmpeg 转 PCM 失败 rc=%d mp3_size=%d stderr=%s",
                r.returncode, len(mp3_data), r.stderr[:300],
            )
            raise RuntimeError(f"ffmpeg 转 PCM 失败 rc={r.returncode}: {r.stderr[:200]}")

        # PCM → SILK（tencent=True 生成微信兼容的 SILK）
        import pilk

        # 诊断：打印 pilk 库实际版本，同样是排查依赖版本漂移
        try:
            pilk_version = getattr(pilk, "__version__", "未知（无 __version__ 属性）")
            log.info("[_convert_mp3_to_silk] pilk 库版本诊断 version=%s", pilk_version)
        except Exception as ver_err:
            log.warning("[_convert_mp3_to_silk] 获取 pilk 版本失败（不影响转码流程）: %s", ver_err, exc_info=True)

        duration_s  = pilk.encode(temp_pcm, temp_silk, pcm_rate=24000, tencent=True)
        playtime_ms = int(duration_s * 1000)

        with open(temp_silk, "rb") as f:
            silk_data = f.read()

        # 诊断：打印生成的 SILK 文件头部字节。微信兼容格式应在标准 SILK 文件
        # （以 "#!SILK_V3" 开头）之前插入一个 0x02 字节，用于确认 tencent=True
        # 是否真的按预期生成了微信兼容格式，而不是普通 SILK 格式
        try:
            header_bytes = silk_data[:12]
            log.info(
                "[_convert_mp3_to_silk] SILK 文件头部诊断 hex=%s ascii_repr=%r",
                header_bytes.hex(), header_bytes,
            )
        except Exception as header_err:
            log.warning("[_convert_mp3_to_silk] 打印 SILK 文件头失败（不影响转码流程）: %s", header_err, exc_info=True)

        log.info("[_convert_mp3_to_silk] 转码完成 size=%d playtime=%dms", len(silk_data), playtime_ms)
        return silk_data, playtime_ms
    finally:
        for p in (temp_mp3, temp_pcm, temp_silk):
            try:
                os.remove(p)
            except OSError:
                pass


async def _wx_upload_to_cdn(mp3_data: bytes, to_user_id: str) -> dict | None:
    """
    加密 MP3 并上传到微信 CDN，返回 voice_item 所需参数。
    流程：随机 AES key → AES-128-ECB 加密 → getuploadurl → POST CDN → 返回下载参数
    （CDN 上传方法为 POST——曾尝试改为 PUT，但实测该端点对 PUT 直接返回 404 Not Found，
    已撤回，以实测结果为准，不再采信文档里未经验证的只言片语）
    """
    import hashlib, base64 as _b64
    from utils import _wx_aes_ecb_encrypt

    if not _current_token or not _current_bot_id:
        log.warning("[_wx_upload_to_cdn] 未配置 token，跳过上传 to_user_id=%s", to_user_id)
        return None

    aes_key_raw = os.urandom(16)
    try:
        encrypted = _wx_aes_ecb_encrypt(mp3_data, aes_key_raw)
    except Exception as e:
        log.error("[_wx_upload_to_cdn] AES 加密失败 mp3_size=%d: %s", len(mp3_data), e, exc_info=True)
        return None

    aeskey_hex = aes_key_raw.hex()
    filekey    = uuid.uuid4().hex
    raw_md5    = hashlib.md5(mp3_data).hexdigest()
    raw_size   = len(mp3_data)
    enc_size   = len(encrypted)

    # 1. getuploadurl（对瞬时网络异常做有限重试，业务错误/其他异常不重试）
    req_url = f"{_current_baseurl}/ilink/bot/getuploadurl"
    payload = {
        "filekey":       filekey,
        "media_type":    4,
        "to_user_id":    to_user_id,
        "rawsize":       raw_size,
        "rawfilemd5":    raw_md5,
        "filesize":      enc_size,
        "aeskey":        aeskey_hex,
        "no_need_thumb": True,
        "base_info":     _base_info(),
    }
    data = None
    network_attempt = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(req_url, headers=_headers(), json=payload)
                data = resp.json()
            break
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if network_attempt < _WX_NETWORK_MAX_RETRIES:
                network_attempt += 1
                log.warning(
                    "[_wx_upload_to_cdn] getuploadurl 网络异常，%ss 后第%d次重试 to_user_id=%s: %s",
                    _WX_NETWORK_RETRY_DELAY, network_attempt, to_user_id, e,
                )
                await asyncio.sleep(_WX_NETWORK_RETRY_DELAY)
                continue
            log.error(
                "[_wx_upload_to_cdn] getuploadurl 网络异常重试%d次后仍失败 to_user_id=%s: %s",
                _WX_NETWORK_MAX_RETRIES, to_user_id, e, exc_info=True,
            )
            return None
        except Exception as e:
            log.error("[_wx_upload_to_cdn] getuploadurl 异常 to_user_id=%s: %s", to_user_id, e, exc_info=True)
            return None

    log.info("[_wx_upload_to_cdn] getuploadurl 完整响应 to_user_id=%s resp=%r", to_user_id, data)
    if data.get("ret", 0) != 0:
        log.error("[_wx_upload_to_cdn] getuploadurl 失败 to_user_id=%s resp=%r", to_user_id, data)
        return None
    upload_param = data.get("upload_param", "")
    if not upload_param:
        log.error("[_wx_upload_to_cdn] getuploadurl 返回空 upload_param to_user_id=%s resp=%r", to_user_id, data)
        return None

    # 2. 上传加密数据到 CDN（同样只对瞬时网络异常重试）
    cdn_base   = os.environ.get("WX_CDN_BASEURL", "https://novac2c.cdn.weixin.qq.com/c2c").rstrip("/")
    cdn_upload = f"{cdn_base}/upload?encrypted_query_param={upload_param}&filekey={filekey}"
    x_param = ""
    network_attempt = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                up_resp = await client.post(
                    cdn_upload,
                    content=encrypted,
                    headers={"Content-Type": "application/octet-stream"},
                )
                try:
                    body_preview = up_resp.content[:500]
                    log.info(
                        "[_wx_upload_to_cdn] 上传响应完整信息 to_user_id=%s status=%d body_len=%d "
                        "body_preview=%r headers=%r",
                        to_user_id, up_resp.status_code, len(up_resp.content), body_preview,
                        dict(up_resp.headers),
                    )
                except Exception as body_log_err:
                    log.warning(
                        "[_wx_upload_to_cdn] 打印上传响应体失败（不影响后续流程）to_user_id=%s: %s",
                        to_user_id, body_log_err, exc_info=True,
                    )
                up_resp.raise_for_status()
                x_param = up_resp.headers.get("x-encrypted-param", "")
            break
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if network_attempt < _WX_NETWORK_MAX_RETRIES:
                network_attempt += 1
                log.warning(
                    "[_wx_upload_to_cdn] CDN 上传网络异常，%ss 后第%d次重试 to_user_id=%s enc_size=%d: %s",
                    _WX_NETWORK_RETRY_DELAY, network_attempt, to_user_id, enc_size, e,
                )
                await asyncio.sleep(_WX_NETWORK_RETRY_DELAY)
                continue
            log.error(
                "[_wx_upload_to_cdn] CDN 上传网络异常重试%d次后仍失败 to_user_id=%s enc_size=%d: %s",
                _WX_NETWORK_MAX_RETRIES, to_user_id, enc_size, e, exc_info=True,
            )
            return None
        except Exception as e:
            log.error("[_wx_upload_to_cdn] 上传失败 to_user_id=%s enc_size=%d: %s", to_user_id, enc_size, e, exc_info=True)
            return None

    if not x_param:
        log.error("[_wx_upload_to_cdn] 响应缺少 x-encrypted-param to_user_id=%s", to_user_id)
        return None

    cdn_base  = os.environ.get("WX_CDN_BASEURL", "https://novac2c.cdn.weixin.qq.com/c2c").rstrip("/")
    full_url  = f"{cdn_base}/download?encrypted_query_param={x_param}"
    return {
        "aeskey": aeskey_hex,
        "media": {
            "encrypt_query_param": x_param,
            "aes_key":             _b64.b64encode(aes_key_raw).decode(),
            "encrypt_type":        1,
            "full_url":            full_url,
        },
    }


async def send_wx_voice_message(to_user_id: str, context_token: str, text: str, retries: int = 1):
    """TTS 合成后通过 iLink 以语音消息（item type=3）发送给用户。
    retries: 瞬时网络异常（连接超时/连接失败）的最大重试次数，业务错误不受此参数影响。"""
    if not _current_token or not _current_bot_id:
        log.warning("[send_wx_voice_message] 未配置 token，跳过 to_user_id=%s", to_user_id)
        return
    if not isinstance(context_token, str) or not context_token:
        log.error(
            "[send_wx_voice_message] context_token 无效，无法发送 to_user_id=%s "
            "context_token_type=%s context_token_repr=%r",
            to_user_id, type(context_token).__name__, context_token,
        )
        return

    mp3_data = await _wx_tts_to_mp3(text)
    if not mp3_data:
        log.error("[send_wx_voice_message] TTS 失败，跳过 to_user_id=%s text_len=%d", to_user_id, len(text))
        return
    log.info("[send_wx_voice_message] TTS 完成 to_user_id=%s size=%d bytes", to_user_id, len(mp3_data))

    # MP3 → SILK（微信原生格式，encode_type=6=SILK，依据官方 Tencent/openclaw-weixin
    # 源码 src/api/types.ts 注释：1=pcm 2=adpcm 3=feature 4=speex 5=amr 6=silk 7=mp3 8=ogg-speex）
    try:
        silk_data, playtime_ms = await asyncio.to_thread(_convert_mp3_to_silk, mp3_data)
    except Exception as e:
        log.error(
            "[send_wx_voice_message] SILK 转码失败 to_user_id=%s mp3_size=%d: %s",
            to_user_id, len(mp3_data), e, exc_info=True,
        )
        return

    audio_data   = silk_data
    encode_type  = 6
    sample_rate  = 24000

    cdn_params = await _wx_upload_to_cdn(audio_data, to_user_id)
    if not cdn_params:
        log.error("[send_wx_voice_message] CDN 上传失败，跳过 to_user_id=%s silk_size=%d", to_user_id, len(silk_data))
        return

    url = f"{_current_baseurl}/ilink/bot/sendmessage"
    payload = {
        "msg": {
            "from_user_id":  "",
            "to_user_id":    to_user_id,
            "client_id":     str(uuid.uuid4()),
            "message_type":  2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [{
                "type": 3,
                "voice_item": {
                    "media":           cdn_params["media"],
                    "encode_type":     encode_type,
                    "bits_per_sample": 16,
                    "sample_rate":     sample_rate,
                    "playtime":        playtime_ms,
                    "text":            text,
                },
            }],
        },
        "base_info": _base_info(),
    }
    other_attempt = 0          # 网络异常专用重试计数，对应 retries 参数
    rate_limit_attempt = 0
    rate_limit_delay = _WX_RATE_LIMIT_BASE_DELAY

    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=_headers(), json=payload)
                data = resp.json()

            if data.get("errcode") == -14:
                log.error("[send_wx_voice_message] sendmessage session 过期，请重新扫码 to_user_id=%s", to_user_id)
                return

            ret = data.get("ret", 0)

            if ret == 0:
                log.info("[send_wx_voice_message] → %s 语音发送成功", to_user_id)
                return

            if ret == -2:
                if rate_limit_attempt < _WX_RATE_LIMIT_MAX_RETRIES:
                    rate_limit_attempt += 1
                    log.warning(
                        "[send_wx_voice_message] sendmessage 被限流（ret=-2），%ss 后第%d次重试 to_user_id=%s",
                        rate_limit_delay, rate_limit_attempt, to_user_id,
                    )
                    await asyncio.sleep(rate_limit_delay)
                    rate_limit_delay = min(rate_limit_delay * 2, _WX_RATE_LIMIT_MAX_DELAY)
                    continue
                log.error(
                    "[send_wx_voice_message] sendmessage 限流重试%d次后仍失败 to_user_id=%s resp=%r",
                    _WX_RATE_LIMIT_MAX_RETRIES, to_user_id, data,
                )
                return

            voice_item = payload["msg"]["item_list"][0]["voice_item"]
            log.error(
                "[send_wx_voice_message] sendmessage 失败 to_user_id=%s http_status=%s ret=%s errcode=%s "
                "context_token_type=%s context_token_len=%d encode_type=%s sample_rate=%s playtime_ms=%s "
                "media_query_param_len=%d resp=%r",
                to_user_id, resp.status_code, data.get("ret"), data.get("errcode"),
                type(context_token).__name__, len(context_token),
                voice_item["encode_type"], voice_item["sample_rate"], voice_item["playtime"],
                len(voice_item["media"].get("encrypt_query_param", "")),
                data,
            )
            return

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if other_attempt < retries:
                other_attempt += 1
                log.warning(
                    "[send_wx_voice_message] sendmessage 第%d次网络异常，重试中 to_user_id=%s: %s",
                    other_attempt, to_user_id, e,
                )
                continue
            log.error(
                "[send_wx_voice_message] sendmessage 网络异常重试%d次后仍失败 to_user_id=%s: %s",
                retries, to_user_id, e, exc_info=True,
            )
            return

        except Exception as e:
            log.error(
                "[send_wx_voice_message] sendmessage 异常 to_user_id=%s: %s",
                to_user_id, e, exc_info=True,
            )
            return
