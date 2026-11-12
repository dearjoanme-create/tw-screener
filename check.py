"""盤中執行：抓前一晚預備名單的即時報價，看今天是不是正在走出合格的進場 K。
  10:55 → 預警（還早，僅供參考）＋持股提醒，只推給你
  13:00 → 確認（離收盤 30 分鐘，可以準備下單），也推給 LINE_SHARE_IDS 裡的人；
          持股有出場／警示時，另外推一則只給你
  12:00～12:59 執行的（測試或手動補跑）算「盤中」，不會影響 13:00 的確認
進場以「收盤確認」為準，盤中結果仍要看 13:30 收盤價。

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
import holdings
import journal
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
    """一檔預備股，用目前報價判斷今天的 K 棒是否合格。回傳 (狀態, 未符合的條件, 預估量, 參考)。
    KD 只當參考，不擋進場。"""
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
            miss.append(f"今天{'漲' if chg >= 0 else '跌'} {abs(chg):.1%}，要漲 2% 以上")
        if price <= op:
            miss.append("目前收黑K（要收紅K）")
        if rng > 0 and (hi - max(op, price)) / rng > C.MAX_SHADOW:
            miss.append("上影線太長（衝上去又被賣下來）")
    else:
        reached = price <= r["trigger"]
        touched = lo <= r["trigger"]
        if chg > -C.ENTRY_MIN_CHG:
            miss.append(f"今天{'跌' if chg < 0 else '漲'} {abs(chg):.1%}，要跌 2% 以上")
        if price >= op:
            miss.append("目前收紅K（要收黑K）")
        if rng > 0 and (min(op, price) - lo) / rng > C.MAX_SHADOW:
            miss.append("下影線太長（跌下去又被買回來）")
    notes = []
    ref = r.get("ref") or {}
    if "k" in ref:
        k2 = next_k(ref["k"], ref["low_n1"], ref["high_n1"], price, lo, hi)
        if side > 0 and k2 <= ref["k"]:
            notes.append("KD 還沒轉強")
        if side < 0 and k2 >= ref["k"]:
            notes.append("KD 還沒轉弱")
    base = r.get("vol_ref")  # 昨天的量（新版）；舊名單沒有就用 5 日均量
    if r.get("need_vol"):
        if base:
            if proj < r["need_vol"] * base:
                miss.append(f"量能不夠（預估 {int(proj):,} 張，要昨天的 {r['need_vol']} 倍 {int(r['need_vol'] * base):,} 張）")
        elif proj < r["need_vol"] * r["vol5"]:
            miss.append(f"量能不夠（預估只有平常的 {proj / max(r['vol5'], 1):.1f} 倍，要 {r['need_vol']} 倍）")
    if reached and not miss:
        status = "成形"
    elif reached:
        status = "價到條件不足"
    elif touched:
        status = "曾到拉回"
    else:
        status = "未到"
    return status, miss, proj, notes


def message(now, slot, groups, total, why=""):
    """盤中通知：第一行就寫結論，原因用白話。"""
    ok, near, back = groups["成形"], groups["價到條件不足"], groups["曾到拉回"]
    title = {"預警": "盤中預警", "盤中": "盤中檢查", "確認": "收盤前確認"}[slot]
    if slot == "確認":
        verdict = (f"{len(ok)} 檔可以進場，收盤前下單；其他先不動" if ok
                   else "今天沒有可以進場的股票，不用動作")
        ok_head = "✅ 可以進場（收盤前下單）"
    else:
        verdict = (f"目前 {len(ok)} 檔符合條件，13:00 再確認，先不用下單" if ok
                   else "目前沒有符合條件的股票，13:00 再看")
        ok_head = "✅ 目前符合（13:00 再確認）"
    if total == 0:
        verdict = "今天沒有需要檢查的股票，不用動作"
    lines = [f"⏰ {now:%m/%d %H:%M} {title}", f"📌 結論：{verdict}"]
    if total == 0:
        return lines + ([why] if why else [])

    def head(x):
        return f"{x['code']} {x['name']}｜{'做多' if x['side'] == 'long' else '放空'}｜{x['title']}"

    if ok:
        lines.append(f"\n{ok_head}")
        for x in ok:
            word = "突破" if x["side"] == "long" else "跌破"
            lines += [head(x), f"  現價 {x['price']:g}，已{word}觸發價 {x['trigger']:g}",
                      f"  停損 {x['stop']:g}" + (f"（{x['stop_method']}）" if x.get("stop_method") else "")
                      + (f"｜{x['term']}" if x.get("term") else "")]
            if x.get("note"):
                lines.append(f"  參考：{x['note']}")
    if near:
        lines.append("\n⚠️ 價格到了，但先不要進場")
        for x in near:
            lines += [head(x), f"  原因：{'、'.join(x['miss'])}"]
    if back:
        lines.append("\n↩️ 不進場：曾經到過觸發價又彈回")
        for x in back:
            lines.append(f"{head(x)}｜現價 {x['price']:g}（觸發 {x['trigger']:g}）")
    rest = total - len(ok) - len(near) - len(back)
    if rest > 0:
        lines.append(f"\n其餘 {rest} 檔還沒到觸發價")
    lines.append("\n以 13:30 收盤價為準" if slot == "確認" else "\n量能是用目前成交量推估全天，早盤通常偏高")
    return lines


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
    held = holdings.fetch()  # 持股只推給你：10:55 併入預警；13:00 另發一則（只在有出場／警示時）
    why = ""
    if not items:  # 沒有股票要檢查也照樣推一則，才分得出「沒事」和「壞掉」
        side_name = {"long": "多方", "short": "空方"}
        if len(sides) == 1:
            other = "short" if sides[0] == "long" else "long"
            why = (f"大盤在月線{'上只看多方' if sides[0] == 'long' else '下只看空方'}，"
                   f"{side_name[sides[0]]}預備 0 檔")
            n = len(cand.get(other, []))
            if n:
                why += f"（{side_name[other]} {n} 檔逆大盤，未檢查）"
        else:
            why = "多方、空方預備都是 0 檔"

    want = {r["code"]: r["market"] for r in items}
    want.setdefault("t00", "tse")  # 加權指數，用來判斷今天有沒有開盤
    for h in held or []:
        want.setdefault(h["code"], h.get("market") or "tse")
    quotes = fetch_quotes(list(want.items()))
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
        status, miss, proj, notes = judge(r, q, elapsed)
        row = {"code": r["code"], "name": r["name"], "side": r["side"], "title": r["title"],
               "trigger": r["trigger"], "price": q["price"], "status": status, "miss": miss,
               "proj_vol": int(proj), "stop": r["stop"].get("use") or r["stop"]["fixed"],
               "stop_method": r["stop"].get("method", ""), "note": "、".join(notes), "term": r.get("term", "")}
        run["results"].append(row)
        if status in groups:
            groups[status].append(row)

    alerts = []
    for h in held or []:
        q = quotes.get(h["code"])
        if q and q["price"]:
            got = holdings.live_check(h, q["price"])
            if got:
                alerts.append((got, h, q["price"]))
    alerts.sort(key=lambda x: x[0][0] != "出場")

    def hold_lines():
        out = []
        for (level, text), h, price in alerts:
            side = "多" if h.get("side", "long") == "long" else "空"
            out.append(f"{holdings.ICON[level]}{h['code']} {h.get('name', '')} {side} 現{price:g}｜{text}")
        return out

    share = slot == "確認"  # 13:00 確認也推給 LINE_SHARE_IDS，所以不放持股
    if items and not run["results"]:
        why = "抓不到即時報價，請到網頁或 GitHub Actions 看看"
    lines = message(now, slot, groups, len(run["results"]), why)
    if lines and held and not share:
        block = [f"\n📋 持股提醒（{len(held)} 檔）"] + (hold_lines() or ["都沒有碰到停損、停利條件，續抱"])
        lines[2:2] = block  # 放在結論下面
    url = line_push.dashboard_url()
    if lines and url:
        lines.append(url)

    # 持股是私人資料，不寫進 checks.json（網頁是公開的）
    checks["runs"].append(run)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(checks, f, ensure_ascii=False, indent=1)

    try:
        journal.log_check(now.strftime("%Y-%m-%d"), run, cand["data_date"])  # 寫進你的 Google 試算表
    except Exception as e:  # noqa: BLE001
        print(f"紀錄表更新失敗（不影響通知）：{type(e).__name__} {e}")

    ok = True
    if lines:
        ok = line_push.push("\n".join(lines), share=share)
    if share and alerts:
        private = [f"📋 持股提醒 {now:%H:%M}（只有你收到）", "收盤 13:30 前還來得及處理："] + hold_lines()
        ok = line_push.push("\n".join(private), share=False) and ok
    if not ok:
        sys.exit(1)  # 讓 GitHub 顯示紅叉提醒你，但結果已經存好了

if __name__ == "__main__":
    main()
