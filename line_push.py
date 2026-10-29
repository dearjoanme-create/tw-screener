"""用 LINE 官方帳號（Messaging API）推播給自己。
需要兩個環境變數（放在 GitHub Secrets）：
  LINE_CHANNEL_ACCESS_TOKEN、LINE_USER_ID
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


def push(text: str):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user = os.environ.get("LINE_USER_ID")
    if not token or not user:
        print("（沒有設定 LINE 金鑰，只印出訊息）\n" + text)
        return
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)][:5]
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": user, "messages": [{"type": "text", "text": t} for t in chunks]},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"LINE 推播失敗 {r.status_code}: {r.text}")
    print("LINE 推播成功")
