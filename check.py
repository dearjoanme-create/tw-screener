"""盤中執行（11:00、13:00）：抓前一晚候選股的即時報價，看有沒有突破觸發價。"""
import datetime as dt
import json
import os
import time
from zoneinfo import ZoneInfo

import requests

import line_push
from patterns import PATTERNS

TW = ZoneInfo("Asia/Taipei")
DOCS = os.path.join(os.path.dirname(__file__), "docs")
MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {"User-Agent": "Mozilla/5.0 (tw-screener)", "Referer": "https://mis.twse.com.tw/stock/index.jsp"}


def _f(s):
    try:
        x = float(str(s).split("_")[0])
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def fetch_quotes(stocks):
    """stocks: [(code, market)] → {code: quote}"""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=20)  # 先拿 cookie
    out = {}
    for i in range(0, len(stocks), 50):
        batch = stocks[i:i + 50]
        ex_ch = "|".join(f"{m}_{c}.tw" for c, m in batch)
        r = s.get(MIS, params={"ex_ch": ex_ch, "json": "1", "delay": "0",
                               "_": int(time.time() * 1000)}, timeout=20)
        for q in r.json().get("msgArray", []):
            price = _f(q.get("z")) or _f(q.get("b")) or _f(q.get("a"))  # 沒成交就用買價
            out[q["c"]] = {
                "price": price,
                "high": _f(q.get("h")),
                "vol": _f(q.get("v")) or 0,  # 累計成交量（張）
                "date": q.get("d"),
                "time": q.get("t"),
            }
        time.sleep(1)
    return out


def main():
    now = dt.datetime.now(TW)
    with open(os.path.join(DOCS, "candidates.json"), encoding="utf-8") as f:
        cand = json.load(f)
    if cand["data_date"] >= now.strftime("%Y-%m-%d"):
        print("候選清單是今天收盤後才產生的，不是給今天用的，跳過。")
        return

    stocks = {}
    for items in cand["patterns"].values():
        for r in items:
            stocks[r["code"]] = r["market"]
    if not stocks:
        print("沒有候選股。")
        return

    quotes = fetch_quotes(list(stocks.items()))
    today = now.strftime("%Y%m%d")
    if not any(q["date"] == today for q in quotes.values()):
        print("今天沒有開盤（假日或颱風假），跳過。")
        return

    # 開盤到現在經過幾分鐘（09:00–13:30 共 270 分鐘），用來預估全日量
    elapsed = (now.hour * 60 + now.minute) - 9 * 60
    elapsed = min(max(elapsed, 1), 270)

    run = {"time": now.strftime("%H:%M"), "results": {}}
    lines = [f"⏰ {now:%m/%d %H:%M} 突破檢查"]
    total_hits = 0
    for key, (title, _) in PATTERNS.items():
        hits, rows = [], []
        for r in cand["patterns"][key]:
            q = quotes.get(r["code"])
            if not q or not q["price"]:
                continue
            proj = q["vol"] * 270 / elapsed
            need = r["vol_ratio"] * r["vol5"]
            vol_ok = r["vol_ratio"] == 0 or proj >= need
            if q["price"] >= r["trigger"]:
                status = "突破"
            elif q["high"] and q["high"] >= r["trigger"]:
                status = "曾突破拉回"
            else:
                status = "未突破"
            row = {
                "code": r["code"], "name": r["name"], "trigger": r["trigger"],
                "price": q["price"], "high": q["high"], "status": status,
                "vol": int(q["vol"]), "proj_vol": int(proj), "vol_ok": vol_ok,
            }
            rows.append(row)
            if status != "未突破":
                hits.append(row)
        run["results"][key] = rows
        if hits:
            total_hits += len(hits)
            lines.append(f"\n【{title}】")
            for h in sorted(hits, key=lambda x: (x["status"] != "突破", not x["vol_ok"])):
                mark = "✅" if h["status"] == "突破" else "↩️"
                vol = "量OK" if h["vol_ok"] else "⚠️量不足"
                lines.append(f"{mark}{h['code']} {h['name']} 現{h['price']:g}／觸發{h['trigger']:g} {vol}")

    if total_hits == 0:
        lines.append("目前沒有候選股突破觸發價。")
    lines.append("\n✅已突破 ↩️曾突破但拉回；量能是依目前成交量推估全日量")
    url = line_push.dashboard_url()
    if url:
        lines.append(url)

    path = os.path.join(DOCS, "checks.json")
    try:
        with open(path, encoding="utf-8") as f:
            checks = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        checks = {"runs": []}
    if checks.get("for_data_date") != cand["data_date"]:
        checks = {"for_data_date": cand["data_date"], "runs": []}
    checks["runs"].append(run)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(checks, f, ensure_ascii=False, indent=1)

    line_push.push("\n".join(lines))


if __name__ == "__main__":
    main()
