"""盤中執行：抓前一晚預備名單的即時報價，看今天是不是正在走出合格的進場 K。
  10:55 → 預警（還早，僅供參考）
  13:00 → 確認（離收盤 30 分鐘，可以準備下單）
  12:00～12:59 執行的（測試或手動補跑）算「盤中」，不會影響 13:00 的確認
朱家泓的進場以「收盤確認」為準，盤中結果仍要看 13:30 收盤價。

預警、確認這兩個時段，同一天各只推一次，所以外部定時器和 GitHub 排程可以同時開著當備援。
手動重跑：python check.py --force
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

import config as C
import line_push
from indicators import next_k

TW = ZoneInfo("Asia/Taipei")
DOCS = os.path.join(os.path.dirname(__file__), "docs")
MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {"User-Agent": "Mozilla/5.0 (tw-screener)", "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
TRADING_MIN = 265  # 09:00～13:25 連續交易的分鐘數（13:25～13:30 是收盤集合競價）


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
                "open": _f(q.get("o")),
                "high": _f(q.get("h")),
                "low": _f(q.get("l")),
                "prev": _f(q.get("y")),
                "vol": _f(q.get("v")) or 0,  # 累計成交量（張）
                "date": q.get("d"),
                "time": q.get("t"),
            }
        time.sleep(1)
    return out


def slot_of(now):
    """依執行時間分時段：12 點前預警、12 點那一小時算盤中、13 點後確認。"""
    if now.hour < 12:
        return "預警"
    if now.hour == 12:
        return "盤中"
    return "確認"


def judge(r, q, elapsed):
    """一檔預備股，用目前報價判斷今天的 K 棒是否合格。回傳 (狀態, 未符合的條件, 預估量)。"""
    side = 1 if r["side"] == "long" else -1
    price, op = q["price"], q["open"] or q["price"]
    hi, lo = q["high"] or price, q["low"] or price
    prev = q["prev"] or r["close"]
    proj = q["vol"] * TRADING_MIN / elapsed
    rng = hi - lo
    chg = price / prev - 1
    miss = []
    if side > 0:
        reached = price >= r["trigger"]
        touched = hi >= r["trigger"]
        if chg < C.ENTRY_MIN_CHG:
            miss.append(f"今天{'漲' if chg >= 0 else '跌'} {abs(chg):.1%}，做多要漲 2% 以上")
        if price <= op:
            miss.append("不是紅K")
        if rng > 0 and (hi - max(op, price)) / rng > C.MAX_SHADOW:
            miss.append("上影線超過一半")
    else:
        reached = price <= r["trigger"]
        touched = lo <= r["trigger"]
        if chg > -C.ENTRY_MIN_CHG:
            miss.append(f"今天{'跌' if chg < 0 else '漲'} {abs(chg):.1%}，放空要跌 2% 以上")
        if price >= op:
            miss.append("不是黑K")
        if rng > 0 and (min(op, price) - lo) / rng > C.MAX_SHADOW:
            miss.append("下影線超過一半")
    ref = r.get("ref") or {}
    if "k" in ref:
        k2 = next_k(ref["k"], ref["low_n1"], ref["high_n1"], price, lo, hi)
        if side > 0 and k2 <= ref["k"]:
            miss.append("K值沒有向上")
        if side < 0 and k2 >= ref["k"]:
            miss.append("K值沒有向下")
    if r.get("need_vol") and proj < r["need_vol"] * r["vol5"]:
        miss.append(f"預估量 {proj / max(r['vol5'], 1):.1f} 倍，不到 {r['need_vol']} 倍")
    if reached and not miss:
        status = "成形"
    elif reached:
        status = "價到條件不足"
    elif touched:
        status = "曾到拉回"
    else:
        status = "未到"
    return status, miss, proj


def main():
    force = "--force" in sys.argv
    now = dt.datetime.now(TW)
    with open(os.path.join(DOCS, "candidates.json"), encoding="utf-8") as f:
        cand = json.load(f)
    if cand["data_date"] >= now.strftime("%Y-%m-%d"):
        print("預備名單是今天收盤後才產生的，不是給今天用的，跳過。")
        return
    slot = slot_of(now)

    path = os.path.join(DOCS, "checks.json")
    try:
        with open(path, encoding="utf-8") as f:
            checks = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        checks = {"runs": []}
    if checks.get("for_data_date") != cand["data_date"]:
        checks = {"for_data_date": cand["data_date"], "runs": []}
    if not force and slot != "盤中" and any(x.get("slot") == slot for x in checks["runs"]):
        print(f"今天的「{slot}」已經推過了，跳過（避免重複通知）。要重跑請加 --force")
        return

    mkt = cand.get("market") or {}
    sides = [s for s in ("long", "short") if mkt.get(f"allow_{s}", True)]
    items = [r for s in sides for r in cand.get(s, [])]
    if not items:
        print("沒有預備股。")
        return

    quotes = fetch_quotes(list({r["code"]: r["market"] for r in items}.items()))
    today = now.strftime("%Y%m%d")
    if not any(q["date"] == today for q in quotes.values()):
        print("今天沒有開盤（假日或颱風假），跳過。")
        return

    elapsed = min(max((now.hour * 60 + now.minute) - 9 * 60, 1), TRADING_MIN)
    run = {"time": now.strftime("%H:%M"), "slot": slot, "results": []}
    groups = {"成形": [], "價到條件不足": [], "曾到拉回": []}
    for r in items:
        q = quotes.get(r["code"])
        if not q or not q["price"]:
            continue
        status, miss, proj = judge(r, q, elapsed)
        row = {"code": r["code"], "name": r["name"], "side": r["side"], "title": r["title"],
               "trigger": r["trigger"], "price": q["price"], "status": status, "miss": miss,
               "proj_vol": int(proj), "stop": r["stop"]["fixed"]}
        run["results"].append(row)
        if status in groups:
            groups[status].append(row)

    note = {"預警": "預警（還早，僅供參考）", "盤中": "盤中檢查", "確認": "確認（以 13:30 收盤為準）"}[slot]
    lines = [f"⏰ {now:%m/%d %H:%M} {note}"]
    label = {"成形": "✅ 可以進場（條件都符合）", "價到條件不足": "⚠️ 價格到了，但K棒不合格", "曾到拉回": "↩️ 曾經到過觸發價又彈回"}
    for status, rows in groups.items():
        if not rows:
            continue
        lines.append(f"\n{label[status]}")
        for x in rows:
            side = "多" if x["side"] == "long" else "空"
            extra = f"｜{'、'.join(x['miss'])}" if x["miss"] else ""
            lines.append(f"{x['code']} {x['name']} {side} 現{x['price']:g}／觸發{x['trigger']:g}{extra}")
            if status == "成形":
                lines.append(f"  停損 {x['stop']:g}")
    if not any(groups.values()):
        lines.append("目前沒有預備股接近進場條件。")
    lines.append("\n量能是用目前成交量推估全天，早盤通常偏高")
    url = line_push.dashboard_url()
    if url:
        lines.append(url)

    checks["runs"].append(run)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(checks, f, ensure_ascii=False, indent=1)

    if not line_push.push("\n".join(lines)):
        sys.exit(1)  # 讓 GitHub 顯示紅叉提醒你，但結果已經存好了


if __name__ == "__main__":
    main()
