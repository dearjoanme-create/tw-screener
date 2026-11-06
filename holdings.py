"""持股追蹤（第 4 階段）：依朱家泓 11-2 進場實戰操作 SOP 的短線／長線／飆股守則，判斷每檔持股該續抱、減碼還是出場。

持股紀錄存在 Google 試算表（透過 Apps Script），GitHub 用 HOLDINGS_URL + HOLDINGS_SECRET 讀寫：
  GET  ?action=holdings&secret=…   → 目前持股（含上一次的建議）
  POST {"secret":…, "action":"advice", "items":[…]} → 存今天的建議，LINE 輸入「持股」時顯示
"""
import os

import requests

import config as C
from indicators import BEAR, BULL

LEVELS = ["續抱", "警示", "減碼", "出場"]  # 由輕到重
ICON = {"續抱": "🟢", "警示": "🟡", "減碼": "🟠", "出場": "🔴"}
MODE_NAME = {"短": "短線", "長": "長線", "飆": "飆股"}


# ------------------------------------------------------------------
# 跟 Apps Script 溝通
# ------------------------------------------------------------------
def _cfg():
    return os.environ.get("HOLDINGS_URL", "").strip(), os.environ.get("HOLDINGS_SECRET", "").strip()


def fetch():
    """回傳持股清單；沒設定或抓不到就回傳 None（不影響選股）。"""
    url, secret = _cfg()
    if not url or not secret:
        return None
    try:
        r = requests.get(url, params={"action": "holdings", "secret": secret}, timeout=30)
        j = r.json()
        if not j.get("ok"):
            print(f"讀取持股失敗：{j.get('error')}")
            return None
        return j.get("holdings") or []
    except Exception as e:  # noqa: BLE001
        print(f"讀取持股失敗：{type(e).__name__}")
        return None


def save_advice(items):
    url, secret = _cfg()
    if not url or not secret or items is None:
        return False
    try:
        r = requests.post(url, json={"secret": secret, "action": "advice", "items": items}, timeout=30)
        return bool(r.json().get("ok"))
    except Exception as e:  # noqa: BLE001
        print(f"存持股建議失敗：{type(e).__name__}")
        return False


# ------------------------------------------------------------------
# 收盤後判斷（每天晚上）
# ------------------------------------------------------------------
def _entry_index(a, date_str):
    dates = a.df["date"].dt.strftime("%Y-%m-%d").tolist()
    for i, d in enumerate(dates):
        if d >= str(date_str)[:10]:
            return i
    return a.t


def _fmt(x):
    return f"{round(float(x), 2):g}"


def evaluate(a, h):
    """a：這檔股票的 Analysis；h：{code, side, price, date, mode}。
    回傳 {level, text, reasons, gain, stop, close, ma5, sum4, ...}。"""
    t = a.t
    c, o = float(a.c[t]), float(a.o[t])
    side = 1 if h.get("side", "long") == "long" else -1
    mode = h.get("mode") or "短"
    entry = float(h["price"])
    ei = _entry_index(a, h.get("date", ""))
    days = max(0, t - ei)
    gain = (c / entry - 1) if side > 0 else (entry / c - 1)
    stop = entry * (1 - C.STOP_PCT) if side > 0 else entry * (1 + C.STOP_PCT)
    ma5, ma20 = float(a.ma[5][t]), float(a.ma[20][t])
    kb = a.kbar(t)
    vr = a.vol_ratio(t)
    out = []  # (level, reason)

    def add(level, why):
        out.append((level, why))

    # ---------- 共同：停損 ----------
    if side > 0 and c <= stop:
        add("出場", f"收盤跌破停損 {_fmt(stop)}")
    if side < 0 and c >= stop:
        add("出場", f"收盤突破停損 {_fmt(stop)}，回補")
    if gain <= -C.STOP_MAX:
        add("出場", "虧損超過 10%，絕對停損")

    # 進場後的轉折：做多出現頭頭低、做空出現底底高 → 趨勢改變
    if side > 0:
        hs = [p for p in a.heads(t) if p.conf > ei]
        prev = [p for p in a.heads(t) if p.conf <= t]
        if hs and len(prev) >= 2 and prev[-1].price < prev[-2].price:
            add("出場", "收盤出現頭頭低，趨勢改變")
    else:
        ls = [p for p in a.lows(t) if p.conf > ei]
        prev = [p for p in a.lows(t) if p.conf <= t]
        if ls and len(prev) >= 2 and prev[-1].price > prev[-2].price:
            add("出場", "收盤出現底底高，趨勢改變")

    if side > 0 and mode == "長":
        if gain >= 0.10 and c < ma20:
            add("出場", f"漲幅超過 10%，收盤跌破月線 {_fmt(ma20)}，停利")
        if gain >= 0.20 and kb["long_black"] and vr >= C.BIG_VOL and c < a.l[t - 1]:
            add("出場", "獲利超過 20%，大量長黑跌破前一日低點")
        base = float(a.l[max(0, t - 150):t + 1].min())
        if base > 0 and c / base - 1 >= C.LATE_GAIN:
            add("警示", "股價已漲約 1 倍，不做長線（長線守則 8）")
        if not out:
            add("續抱", f"沒跌破月線 {_fmt(ma20)}，長線續抱")

    elif side > 0 and mode == "飆":
        ma3 = float(a.c[t - 2:t + 1].mean())
        if c < min(a.l[t - 1], a.l[t - 2]):
            add("出場", "跌破前 2 日低點")
        if c < ma3:
            add("出場", f"跌破 3 日均線 {_fmt(ma3)}")
        if vr >= C.HUGE_VOL and kb["black"] and o > a.c[t - 1]:
            add("出場", "量大增、開高走低出現黑K，高點已到")
        elif vr >= C.HUGE_VOL and (a.h[t] / a.l[t] - 1) >= 0.07:
            add("減碼", "量放大、價大幅震盪，可先出一半")
        elif kb["black"]:
            add("警示", "出現黑K，留意明天走勢")
        if not out:
            add("續抱", "沒破前 2 日低、沒破 3 日均線、沒出黑K，續抱")

    elif side > 0:  # 短線波段（11-2 短線波段操作守則）
        long_reds = all(a.kbar(k)["long_red"] for k in range(t - 3, t))
        if (gain >= 0.20 or long_reds) and kb["long_black"] and vr >= C.BIG_VOL:
            if c < a.l[t - 1]:
                add("出場", "獲利大、出現大量長黑跌破前一日低點，全部出場（守則 8）")
            else:
                add("減碼", "獲利大、出現大量長黑，先減碼一半，明天開低就全出（守則 7）")
        prev_red = a.c[t - 1] > a.o[t - 1]
        if kb["black"] and prev_red and c < (a.o[t - 1] + a.c[t - 1]) / 2 and a.k[t] < a.k[t - 1]:
            add("減碼", "黑K吃掉前一根紅K一半以上、K值下彎，先賣一半（守則 11）")
        above = [x for x in a.w_heads if x > c]
        if c < ma5 and kb["black"] and above and min(above) / c - 1 <= C.WEEKLY_PRESSURE:
            add("出場", f"接近週線壓力 {_fmt(min(above))}，黑K跌破 5 日線先出場（守則 19）")
        if c < ma5:
            if gain >= 0.10:
                add("出場", f"漲幅超過 10%，收盤跌破 5 日線 {_fmt(ma5)}，停利（守則 6）")
            elif a.ma_down(60, t):
                add("出場", "季線下彎，跌破 5 日線先出場（守則 20）")
            elif kb["chg"] > -0.01 and vr < 1 and a.ma_up(20, t):
                add("警示", "跌破 5 日線但跌幅小、量縮、月線上揚：今天續抱，明天再跌就賣（守則 15）")
            else:
                add("續抱", "漲幅未達 10%，跌破 5 日線續抱（守則 5）")
        if not out:
            if gain >= 0.10:
                add("續抱", f"續抱，收盤跌破 5 日線 {_fmt(ma5)} 就停利（守則 6）")
            else:
                add("續抱", "沒有跌破停損，續抱（守則 4）")
        if gain >= 0.10:
            add("續抱", "漲幅已超過 10%，不宜加碼（守則 18）")

    else:  # 放空
        if c > ma5:
            if gain >= 0.05:
                add("出場", f"獲利超過 5%，收盤站上 5 日線 {_fmt(ma5)}，停利回補")
            else:
                add("續抱", "獲利未達 5%，站上 5 日線續抱")
        if a.trend[t] == BULL:
            add("出場", "已轉多頭，空單回補")
        if not out:
            if gain >= 0.05:
                add("續抱", f"續抱，收盤站上 5 日線 {_fmt(ma5)} 就停利回補")
            else:
                add("續抱", "沒有突破停損，續抱")

    # ---------- 共同：接近停損、汰弱留強 ----------
    near = (c <= stop * 1.02) if side > 0 else (c >= stop * 0.98)
    if near and not any(lv == "出場" for lv, _ in out):
        add("警示", f"接近停損 {_fmt(stop)}")
    if days >= 10 and gain < 0.05 and not any(lv == "出場" for lv, _ in out):
        add("警示", f"持有 {days} 天{'漲' if side > 0 else '跌'}不到 5%，考慮換強勢股（強勢比漲勢重要）")
    if side > 0 and a.trend[t] == BEAR and not any(lv == "出場" for lv, _ in out):
        add("警示", "個股已轉空頭，做多要特別小心")

    level = max((lv for lv, _ in out), key=LEVELS.index)
    main = [w for lv, w in out if lv == level]
    rest = [w for lv, w in out if lv != level]
    return {
        "code": h["code"],
        "side": "long" if side > 0 else "short",
        "mode": mode,
        "date": a.df["date"].iloc[t].strftime("%Y-%m-%d"),
        "level": level,
        "text": "；".join(main),
        "notes": rest,
        "close": round(c, 2),
        "gain": round(gain, 4),
        "stop": round(stop, 2),
        "ma5": round(ma5, 2),
        "sum4": round(float(a.c[t - 3:t + 1].sum()), 2),  # 明天 5 日線 =（這 4 天收盤＋明天價）÷ 5
        "days": days,
    }


# ------------------------------------------------------------------
# 盤中（13:00）：用即時價格檢查停損、停利
# ------------------------------------------------------------------
def live_check(h, price):
    """h：持股（含上一次的 advice）。回傳 (等級, 說明) 或 None（沒事）。"""
    side = 1 if h.get("side", "long") == "long" else -1
    entry = float(h["price"])
    adv = h.get("advice") or {}
    stop = float(adv.get("stop") or (entry * (1 - C.STOP_PCT) if side > 0 else entry * (1 + C.STOP_PCT)))
    gain = (price / entry - 1) if side > 0 else (entry / price - 1)
    ma5 = (float(adv["sum4"]) + price) / 5 if adv.get("sum4") else None
    if side > 0:
        if price <= stop:
            return "出場", f"跌破停損 {_fmt(stop)}，收盤前考慮出場"
        if gain >= 0.10 and ma5 and price < ma5 and (h.get("mode") or "短") == "短":
            return "出場", f"漲幅超過 10%，目前跌破 5 日線 {_fmt(ma5)}，收盤確認就停利"
        if price <= stop * 1.015:
            return "警示", f"接近停損 {_fmt(stop)}"
    else:
        if price >= stop:
            return "出場", f"突破停損 {_fmt(stop)}，收盤前考慮回補"
        if gain >= 0.05 and ma5 and price > ma5:
            return "出場", f"獲利超過 5%，目前站上 5 日線 {_fmt(ma5)}，收盤確認就回補"
        if price >= stop * 0.985:
            return "警示", f"接近停損 {_fmt(stop)}"
    return None


def push_section(results, names):
    """晚上推播裡的「持股檢查」段落。"""
    if not results:
        return []
    order = sorted(results, key=lambda r: -LEVELS.index(r["level"]))
    lines = [f"\n【持股檢查】{len(results)} 檔"]
    for r in order:
        side = "多" if r["side"] == "long" else "空"
        name = names.get(r["code"], "")
        lines.append(f"{ICON[r['level']]}{r['code']} {name} {side}・{MODE_NAME.get(r['mode'], '短線')} "
                     f"{r['gain']:+.1%}｜{r['level']}")
        lines.append(f"  {r['text']}")
    return lines
