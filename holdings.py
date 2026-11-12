"""持股追蹤：依短線／中線／長線／飆股（多空都適用）的操作規則，判斷每檔持股該續抱、減碼還是出場。

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
MODE_NAME = {"短": "短線", "中": "中線", "長": "長線", "飆": "飆股"}


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


def pressure(a, t, price, side, lookback=120):
    """短線獲利目標：做多看向上第 1 個壓力（前高、大量黑K高點、向下跳空缺口、盤整區上緣），
    做空看向下第 1 個支撐。回傳 (價位, 說明) 或 None。"""
    s = max(1, t - lookback)
    cands = []
    if side > 0:
        cands += [(p.price, "前高") for p in a.heads(t) if p.idx >= s]
        for k in range(s, t + 1):
            if a.kbar(k)["black"] and a.vol_ratio(k) >= C.BIG_VOL:
                cands.append((float(a.h[k]), "大量黑K高點"))
            if a.h[k] < a.l[k - 1]:
                cands.append((float(a.l[k - 1]), "跳空缺口"))
        cands = [x for x in cands if x[0] > price * 1.005]
        return min(cands) if cands else None
    cands += [(p.price, "前低") for p in a.lows(t) if p.idx >= s]
    for k in range(s, t + 1):
        if a.kbar(k)["red"] and a.vol_ratio(k) >= C.BIG_VOL:
            cands.append((float(a.l[k]), "大量紅K低點"))
        if a.l[k] > a.h[k - 1]:
            cands.append((float(a.h[k - 1]), "跳空缺口"))
    cands = [x for x in cands if x[0] < price * 0.995]
    return max(cands) if cands else None


def _top_signal(a, k, side, gain_k):
    """急漲 3 天以上中長紅K或獲利 20% 以上，出現高檔爆量長黑（開高走低 5% 以上）或爆量長上影線。
    做空反過來：急跌 3 天以上中長黑K或獲利 20%，出現低檔爆量長紅或長下影線。"""
    kb = a.kbar(k)
    big = a.vol_ratio(k) >= C.BIG_VOL
    if side > 0:
        run = all(a.kbar(j)["long_red"] for j in range(k - 3, k))
        hit = big and ((kb["black"] and (a.o[k] - a.c[k]) / a.o[k] >= 0.05) or kb["upper_ratio"] >= 0.5)
    else:
        run = all(a.kbar(j)["long_black"] for j in range(k - 3, k))
        hit = big and ((kb["red"] and (a.c[k] - a.o[k]) / a.o[k] >= 0.05) or kb["lower_ratio"] >= 0.5)
    return (run or gain_k >= C.BIG_GAIN) and hit


def _engulf(a, k, side):
    """長黑吞噬：長黑K把前一根紅K整根吃掉（做空看長紅吞噬）。"""
    kb, pv = a.kbar(k), a.kbar(k - 1)
    if side > 0:
        return kb["long_black"] and pv["red"] and a.o[k] >= a.c[k - 1] and a.c[k] <= a.o[k - 1]
    return kb["long_red"] and pv["black"] and a.o[k] <= a.c[k - 1] and a.c[k] >= a.o[k - 1]


def evaluate(a, h):
    """a：這檔股票的 Analysis；h：{code, side, price, date, mode, stop?}。
    mode：短（守 5 日線）、中（守 10 日線）、長（守月線）、飆。
    回傳 {level, text, reasons, gain, stop, close, ma5, sum4, target, ...}。"""
    t = a.t
    c, o = float(a.c[t]), float(a.o[t])
    side = 1 if h.get("side", "long") == "long" else -1
    mode = h.get("mode") or "短"
    entry = float(h["price"])
    ei = _entry_index(a, h.get("date", ""))
    days = max(0, t - ei)

    def g_at(k):
        return (a.c[k] / entry - 1) if side > 0 else (entry / a.c[k] - 1)

    gain = g_at(t)
    try:
        stop = float(h.get("stop") or 0) or None
    except (TypeError, ValueError):
        stop = None
    if not stop:
        stop = entry * (1 - C.STOP_PCT) if side > 0 else entry * (1 + C.STOP_PCT)
    ma5, ma10, ma20 = float(a.ma[5][t]), float(a.ma[10][t]), float(a.ma[20][t])
    kb = a.kbar(t)
    vr = a.vol_ratio(t)
    out = []  # (level, reason)

    def add(level, why):
        out.append((level, why))

    def exiting():
        return any(lv == "出場" for lv, _ in out)

    # ---------- 共同：停損 ----------
    if side > 0 and c <= stop:
        add("出場", f"收盤跌破停損 {_fmt(stop)}")
    if side < 0 and c >= stop:
        add("出場", f"收盤突破停損 {_fmt(stop)}，回補")
    if gain <= -C.STOP_MAX:
        add("出場", "虧損超過 10%，絕對停損")

    # 進場後的轉折：做多出現頭頭低、做空出現底底高 → 短線出場，長線續抱
    turned = False
    if side > 0:
        hs = [p for p in a.heads(t) if p.conf > ei]
        prev = [p for p in a.heads(t) if p.conf <= t]
        turned = bool(hs) and len(prev) >= 2 and prev[-1].price < prev[-2].price
        why_turn = "收盤出現頭頭低，趨勢改變"
    else:
        ls = [p for p in a.lows(t) if p.conf > ei]
        prev = [p for p in a.lows(t) if p.conf <= t]
        turned = bool(ls) and len(prev) >= 2 and prev[-1].price > prev[-2].price
        why_turn = "收盤出現底底高，趨勢改變"
    if turned and mode != "長":
        add("出場", why_turn)

    if mode == "飆" and side > 0:
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

    elif mode == "長":
        # 長線守月線；獲利超過 20% 且乖離超過 20%，跌破 10 日線停利；空頭確認先停利
        bias = (c / ma20 - 1) * side
        if (side > 0 and c < ma20) or (side < 0 and c > ma20):
            add("出場", f"收盤{'跌破' if side > 0 else '站上'}月線 {_fmt(ma20)}，長線出場")
        if gain >= C.BIG_GAIN and bias >= C.LONG_BIAS and ((side > 0 and c < ma10) or (side < 0 and c > ma10)):
            add("出場", f"獲利超過 20%、乖離超過 20%，收盤{'跌破' if side > 0 else '站上'} 10 日線 {_fmt(ma10)}，停利")
        if (side > 0 and a.trend[t] == BEAR) or (side < 0 and a.trend[t] == BULL):
            add("出場", "趨勢已反轉確認，先停利出場")
        if turned and not exiting():
            add("警示", why_turn + "，短線出場、長線續抱（跌破盤整再出場）")
        base = float(a.l[max(0, t - 150):t + 1].min())
        if side > 0 and base > 0 and c / base - 1 >= C.LATE_GAIN:
            add("警示", "股價已漲約 1 倍，只適合短線（守 5 日線）")
        if not out:
            add("續抱", f"沒{'跌破' if side > 0 else '站上'}月線 {_fmt(ma20)}，長線續抱")

    else:
        # 短線守 5 日線、中線守 10 日線
        line, name = (ma10, "10 日線") if mode == "中" else (ma5, "5 日線")
        broke = c < line if side > 0 else c > line
        if _top_signal(a, t - 1, side, g_at(t - 1)) and (c < a.c[t - 1] if side > 0 else c > a.c[t - 1]):
            add("出場", "昨天高檔出現爆量長黑／長上影線，今天續跌，全部出場" if side > 0
                else "昨天低檔出現爆量長紅／長下影線，今天續漲，全部回補")
        if _engulf(a, t, side) and (gain >= C.BIG_GAIN or all(a.kbar(j)["long_red" if side > 0 else "long_black"] for j in range(t - 3, t))):
            add("出場", "長黑吞噬，全部停利" if side > 0 else "長紅吞噬，全部回補")
        elif _top_signal(a, t, side, gain):
            add("減碼", "高檔爆量長黑或長上影線，今天先賣一半，明天再跌就全賣" if side > 0
                else "低檔爆量長紅或長下影線，今天先回補一半，明天再漲就全補")
        if side > 0:
            above = [x for x in a.w_heads if x > c]
            if c < ma5 and kb["black"] and above and min(above) / c - 1 <= C.WEEKLY_PRESSURE:
                add("出場", f"接近週線壓力 {_fmt(min(above))}，黑K跌破 5 日線先出場")
        if broke:
            if gain >= C.TAKE_PROFIT:
                add("出場", f"{'漲' if side > 0 else '跌'}幅超過 10%，收盤{'跌破' if side > 0 else '站上'} {name} {_fmt(line)}，停利")
            elif side > 0 and a.ma_down(60, t):
                add("出場", f"季線下彎，跌破 {name}先出場")
            else:
                add("續抱", f"{'漲' if side > 0 else '跌'}幅未達 10%，{'跌破' if side > 0 else '站上'} {name}續抱")
        if (side < 0 and a.trend[t] == BULL):
            add("出場", "已轉多頭，空單回補")
        if not out:
            if gain >= C.TAKE_PROFIT:
                add("續抱", f"續抱，收盤{'跌破' if side > 0 else '站上'} {name} {_fmt(line)} 就停利")
            else:
                add("續抱", "沒有碰到停損，續抱")
        if gain >= C.TAKE_PROFIT and side > 0:
            add("續抱", "漲幅已超過 10%，不宜加碼")

    # ---------- 共同：接近停損、汰弱留強、目標 ----------
    near = (c <= stop * 1.02) if side > 0 else (c >= stop * 0.98)
    if near and not exiting():
        add("警示", f"接近停損 {_fmt(stop)}")
    if days >= 10 and gain < 0.05 and not exiting():
        add("警示", f"持有 {days} 天{'漲' if side > 0 else '跌'}不到 5%，考慮換強勢股（強勢比漲勢重要）")
    if side > 0 and a.trend[t] == BEAR and not exiting() and mode != "長":
        add("警示", "個股已轉空頭，做多要特別小心")
    tgt = pressure(a, t, c, side)
    if tgt and not exiting():
        add("續抱", f"短線目標：{'上方' if side > 0 else '下方'}第 1 個{'壓力' if side > 0 else '支撐'} {_fmt(tgt[0])}（{tgt[1]}），"
                   f"{'突破續抱、遇壓下跌' if side > 0 else '跌破續抱、遇撐反彈'}{'跌破' if side > 0 else '站上'} 5 日線出場")

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
        "target": round(tgt[0], 2) if tgt else None,
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "sum4": round(float(a.c[t - 3:t + 1].sum()), 2),  # 明天 5 日線 =（這 4 天收盤＋明天價）÷ 5
        "sum9": round(float(a.c[t - 8:t + 1].sum()), 2),  # 明天 10 日線
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
    mode = h.get("mode") or "短"
    stop = float(h.get("stop") or adv.get("stop") or (entry * (1 - C.STOP_PCT) if side > 0 else entry * (1 + C.STOP_PCT)))
    gain = (price / entry - 1) if side > 0 else (entry / price - 1)
    if mode == "中" and adv.get("sum9"):
        line, name = (float(adv["sum9"]) + price) / 10, "10 日線"
    elif adv.get("sum4") and mode == "短":
        line, name = (float(adv["sum4"]) + price) / 5, "5 日線"
    else:
        line = None
    if side > 0:
        if price <= stop:
            return "出場", f"跌破停損 {_fmt(stop)}，收盤前考慮出場"
        if gain >= C.TAKE_PROFIT and line and price < line:
            return "出場", f"漲幅超過 10%，目前跌破 {name} {_fmt(line)}，收盤確認就停利"
        if price <= stop * 1.015:
            return "警示", f"接近停損 {_fmt(stop)}"
    else:
        if price >= stop:
            return "出場", f"突破停損 {_fmt(stop)}，收盤前考慮回補"
        if gain >= C.TAKE_PROFIT and line and price > line:
            return "出場", f"獲利超過 10%，目前站上 {name} {_fmt(line)}，收盤確認就回補"
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
