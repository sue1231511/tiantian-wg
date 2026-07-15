"""
微信 iLink 一次性登录脚本。
运行后扫码，自动把 token 写入 Supabase，不需要动环境变量、不需要重部署。
配置方法：设置环境变量 SUPABASE_URL 和 SUPABASE_KEY，然后运行此脚本。
"""
import os
import time
import requests

BASE_URL = "https://ilinkai.weixin.qq.com"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


def get_qrcode() -> tuple[str, str]:
    resp = requests.get(
        f"{BASE_URL}/ilink/bot/get_bot_qrcode",
        params={"bot_type": 3},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["qrcode"], data["qrcode_img_content"]


def poll_status(qrcode: str) -> dict | None:
    print("等待扫码...")
    while True:
        try:
            resp = requests.get(
                f"{BASE_URL}/ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode},
                timeout=(10, 65),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"⚠️  轮询出错: {e}，2 秒后重试")
            time.sleep(2)
            continue

        status = data.get("status", "")
        if status == "confirmed":
            return data
        elif status == "expired":
            return None
        elif status == "scaned":
            print("✅ 已扫码，请在微信上点击确认...")
        else:
            print(f"⏳ 状态：{status}")
        time.sleep(2)


def save_to_supabase(bot_token: str, ilink_bot_id: str, baseurl: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠️  未配置 SUPABASE_URL/KEY，跳过写入 Supabase")
        return False

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    items = [
        ("wx_ilink_token",   bot_token),
        ("wx_ilink_bot_id",  ilink_bot_id),
        ("wx_ilink_baseurl", baseurl),
    ]
    ok = True
    for key, value in items:
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/bot_settings",
                headers=headers,
                json={"key": key, "value": value},
                timeout=8,
            )
            if resp.status_code not in (200, 201):
                print(f"❌ 写入 {key} 失败: {resp.status_code} {resp.text[:100]}")
                ok = False
        except Exception as e:
            print(f"❌ 写入 {key} 异常: {e}")
            ok = False
    return ok


def main():
    print("🔄 正在获取登录二维码...")
    try:
        qrcode, qrcode_url = get_qrcode()
    except Exception as e:
        print(f"❌ 获取二维码失败: {e}")
        return

    print(f"\n📱 请用浏览器打开以下链接，然后用微信扫码：\n")
    print(f"   {qrcode_url}\n")

    result = poll_status(qrcode)
    if not result:
        print("\n❌ 二维码已过期，请重新运行脚本")
        return

    bot_token    = result.get("bot_token", "")
    ilink_bot_id = result.get("ilink_bot_id", "")
    baseurl      = result.get("baseurl", BASE_URL).rstrip("/")

    print("\n✅ 登录成功！")

    # 写入 Supabase（主要方式）
    if SUPABASE_URL and SUPABASE_KEY:
        if save_to_supabase(bot_token, ilink_bot_id, baseurl):
            print("📦 凭证已写入 Supabase，服务器会自动加载，无需重部署。")
        else:
            print("⚠️  Supabase 写入失败，请手动配置以下环境变量：")
            _print_env(bot_token, ilink_bot_id, baseurl)
    else:
        print("⚠️  未配置 Supabase，请手动配置以下环境变量：")
        _print_env(bot_token, ilink_bot_id, baseurl)


def _print_env(bot_token, ilink_bot_id, baseurl):
    print(f"\nWX_ILINK_TOKEN={bot_token}")
    print(f"WX_ILINK_BASEURL={baseurl}")
    print(f"WX_ILINK_BOT_ID={ilink_bot_id}")


if __name__ == "__main__":
    main()
