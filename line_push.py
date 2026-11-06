"""用 LINE 官方帳號（Messaging API）推播。
環境變數（放在 GitHub Secrets）：
  LINE_CHANNEL_ACCESS_TOKEN、LINE_USER_ID（你自己）
  LINE_SHARE_IDS（選填）：一起收 13:00 確認通知的人，多個用逗號隔開
"""
import os

import requests

import config

MAX_LEN = 4900  # LINE 單則文字上限 5000 字


def dashboard_url():
    if config.DASHBOARD_URL:
        return config.DASHBOARD_URL
    repo = os.environ.get("GITHUB_REPOSITORY", "")  # 例如 hy/tw-screener
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


HINT = {
    401: "token 不對：請確認貼的是 Channel access token（很長那串），不是 Channel secret",
    400: "User ID 不對，或對方還沒把官方帳號加為好友",
    429: "本月免費訊息額度用完了",
}


def _send(token, user, chunks):
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": user, "messages": [{"type": "text", "text": t} for t in chunks]},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        print(f"::error::LINE 推播連線失敗：{e}")
        return False
    if r.status_code == 200:
        return True
    print(f"::error::LINE 推播失敗 {r.status_code}（{user[:6]}…）：{HINT.get(r.status_code, '')} {r.text}")
    return False


def share_ids():
    raw = os.environ.get("LINE_SHARE_IDS", "")
    return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]


def push(text: str, share: bool = False) -> bool:
    """推播給你；share=True 時也推給 LINE_SHARE_IDS 裡的人。全部成功才回傳 True。
    失敗不會讓程式中斷，只會在紀錄裡留下錯誤。"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user:
        print("（沒有設定 LINE 金鑰，只印出訊息）\n" + text)
        return True
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)][:5]
    to = [user] + ([x for x in share_ids() if x != user] if share else [])
    ok = [_send(token, u, chunks) for u in to]
    if all(ok):
        print(f"LINE 推播成功（{len(to)} 人）")
        return True
    if not ok[0]:
        print("（資料已照常存檔，修好金鑰後到 Actions 手動重跑即可）\n" + text)
    return False
