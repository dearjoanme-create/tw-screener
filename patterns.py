"""進場位置判斷（朱家泓《活用技術分析寶典》11-1、12-1），加上做多／做空 10 大戒律與短線波段守則。

流程：
  find_setups(a, t)  → 第 t 天收盤後，明天可能出現的進場位置（只差最後一根進場 K）
  evaluate(a, t, rs) → 算出明天的觸發條件、套用戒律、打分數，回傳多方／空方各一筆（或 None）
  confirmed_today(a, rs) → 昨天的預備名單，今天收盤是否已經走出進場 K
"""
import math

import numpy as np

import config as C
from indicators import BEAR, BULL, RANGE, TREND_NAME, position

LONG = {
    "L1": "打底完成突破",
    "L2": "回後買上漲",
    "L3": "突破盤整上頸線",
    "L4": "橫盤／一字底突破",
    "L5": "突破大量黑K",
    "L6": "ABC修正突破切線",
    "L7": "假跌破真上漲",
}
SHORT = {
    "S1": "盤頭確認",
    "S2": "彈後空下跌",
    "S3": "跌破盤整下頸線",
    "S4": "跌破橫盤／一字底",
    "S5": "反彈後跌破紅K低點",
    "S6": "假突破真下跌",
}
TITLES = {**LONG, **SHORT}
# 同一天同一邊有好幾個位置時，先用排在前面的（轉折、突破優先）
ORDER = ["L1", "L6", "L7", "L3", "L4", "L5", "L2", "S1", "S6", "S3", "S4", "S5", "S2"]


# ------------------------------------------------------------------
# 小工具
# ------------------------------------------------------------------
def f2(x):
    """價格顯示用：最多兩位小數。"""
    return f"{round(float(x), 2):g}"


def tick(p):
    """台股升降單位。"""
    if p < 10:
        return 0.01
    if p < 50:
        return 0.05
    if p < 100:
        return 0.1
    if p < 500:
        return 0.5
    if p < 1000:
        return 1.0
    return 5.0


def round_up(p):
    t = tick(p)
    return round(math.ceil(p / t - 1e-9) * t, 2)


def round_down(p):
    t = tick(p)
    return round(math.floor(p / t + 1e-9) * t, 2)


def group(a, t):
    """第 t 天所在的轉折群：(狀態 1=均線上／-1=均線下, 群的第一根)。"""
    last = [p for p in a.pivots if p.conf <= t]
    if not last:
        return None, 0
    p = last[-1]
    return (1 if p.kind == "L" else -1), p.conf


def box(a, t, lookback=60):
    """盤整區：最近兩個頭、兩個底都在 lookback 天內 → (上頸線, 下頸線, 起點)。"""
    hs = [p for p in a.heads(t) if p.idx >= t - lookback][-2:]
    ls = [p for p in a.lows(t) if p.idx >= t - lookback][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return None
    upper = max(p.price for p in hs)
    lower = min(p.price for p in ls)
    if lower <= 0:
        return None
    width = upper / lower - 1
    if not (C.BOX_MIN_WIDTH <= width <= C.BOX_MAX_WIDTH):
        return None
    start = min(p.idx for p in hs + ls)
    return upper, lower, start


def flat_window(a, t, max_n=10):
    """K 線橫盤：結尾在第 t 天、最長的橫盤天數（至少 FLAT_DAYS 天）。"""
    best = None
    for n in range(C.FLAT_DAYS, max_n + 1):
        s = t - n + 1
        if s < 1:
            break
        hi, lo = a.h[s:t + 1].max(), a.l[s:t + 1].min()
        if hi / lo - 1 <= C.FLAT_MAX_WIDTH:
            best = (n, float(hi), float(lo))
        else:
            break
    return best


def tangle_window(a, t):
    """一字底／一字橫盤：近 TANGLE_DAYS 天大多數時候 5、10、20 日線糾結。"""
    n = C.TANGLE_DAYS
    if t < n + 20:
        return None
    days = sum(a.tangled(i) for i in range(t - n + 1, t + 1))
    if days < n * 0.7:
        return None
    s = t - n + 1
    hi, lo = a.h[s:t + 1].max(), a.l[s:t + 1].min()
    if hi / lo - 1 > C.TANGLE_WIDTH:
        return None
    return n, float(hi), float(lo)


def drawdown_from_high(a, t, n=120):
    s = max(0, t - n)
    return 1 - a.c[t] / a.h[s:t + 1].max()


def rise_from_low(a, t, n=120):
    s = max(0, t - n)
    return a.c[t] / a.l[s:t + 1].min() - 1


def had_trend(a, t, kind, n=60):
    return bool((a.trend[max(0, t - n):t + 1] == kind).any())


# ------------------------------------------------------------------
# 進場位置
# ------------------------------------------------------------------
def find_setups(a, t):
    """回傳 [(key, level, stop, need_vol, note)]。level = 型態本身要突破／跌破的價位（沒有就 None）。"""
    out = []
    c, h, l = a.c, a.h, a.l
    tr = a.trend[t]
    st, gs = group(a, t)
    H, L = a.heads(t), a.lows(t)
    ma5, ma20, ma60 = a.ma[5][t], a.ma[20][t], a.ma[60][t]
    bx = box(a, t)

    # ---------- 多方 ----------
    # L1 打底完成：空頭下跌低檔，底不再破，明天突破頸線（最近的頭）→ 多頭確認
    if tr != BULL and len(L) >= 2 and len(H) >= 1 and had_trend(a, t, BEAR) and drawdown_from_high(a, t) >= 0.2:
        l0, l1 = L[-2], L[-1]
        neck = [p.price for p in H if p.idx > l0.idx]
        if l1.price >= l0.price * 0.97 and neck and not a.ma_down(20, t):
            lvl = max(neck)
            if c[t] < lvl:
                out.append(("L1", lvl, l1.price, C.BIG_VOL, f"頸線 {f2(lvl)}，底 {f2(l0.price)}→{f2(l1.price)}"))

    # L2 回後買上漲：多頭回檔，不破前低、不破月線，明天站回 5 日線
    if tr == BULL and st == -1 and c[t] < ma5 and L and a.ma_up(20, t):
        pull_low = float(l[gs:t + 1].min())
        if pull_low > L[-1].price and (c[gs:t + 1] >= a.ma[20][gs:t + 1]).all():
            out.append(("L2", None, pull_low, 1.0, f"回檔低點 {f2(pull_low)}，前低 {f2(L[-1].price)}"))

    # L3 突破盤整上頸線／L7 假跌破真上漲
    if tr == RANGE and bx and not a.ma_down(20, t) and (c[t] > ma60 or a.ma_up(60, t)):
        upper, lower, s = bx
        if lower <= c[t] < upper and not (had_trend(a, t, BEAR) and drawdown_from_high(a, t) >= 0.2):
            fake = (c[max(s, t - 10):t + 1] < lower).any()
            key = "L7" if fake else "L3"
            out.append((key, upper, upper, C.BIG_VOL, f"上頸線 {f2(upper)}／下頸線 {f2(lower)}" + ("，曾假跌破" if fake else "")))

    # L4 K 線橫盤（多頭中）或一字底（均線糾結）突破
    tw = tangle_window(a, t)
    fw = flat_window(a, t)
    if tw:
        n, hi, lo = tw
        out.append(("L4", hi, lo, C.BIG_VOL, f"一字底 {n} 天，{f2(lo)}–{f2(hi)}"))
    elif fw and tr == BULL:
        n, hi, lo = fw
        out.append(("L4", hi, lo, C.BIG_VOL, f"K線橫盤 {n} 天，{f2(lo)}–{f2(hi)}"))

    # L5 突破大量黑K（3 日內）／強勢股回檔 1～2 天續攻
    if tr == BULL or (c[t] > ma20 and a.ma_up(20, t)):
        for k in range(t, max(t - C.REBOUND_DAYS, 1), -1):
            kb = a.kbar(k)
            if not (kb["black"] and kb["chg"] < 0):
                continue
            big = a.vol_ratio(k) >= C.BIG_VOL and (l[k] < l[k - 1] or c[k] < a.ma[5][k])
            strong = c[k - 1] / c[max(0, k - 11)] - 1 >= 0.10
            if (big or strong) and (c[k:t + 1] <= h[k]).all():
                out.append(("L5", float(h[k]), float(l[k:t + 1].min()), C.BIG_VOL,
                            f"{'大量' if big else ''}黑K高點 {f2(h[k])}（{'今天' if k == t else f'{t - k} 天前'}）"))
            break

    # L6 ABC 修正突破下降切線：多頭（月線向上）修正出頭頭低、底底低，明天突破切線
    if a.ma_up(20, t) and c[t] >= ma20:
        recent = [p for p in H if p.idx >= t - 60]
        if len(recent) >= 2:
            A = max(recent, key=lambda p: p.price)
            after = [p for p in recent if p.idx > A.idx and p.price < A.price]
            lows_after = [p for p in L if p.idx > A.idx]
            if after and len(lows_after) >= 2 and lows_after[-1].price < lows_after[-2].price:
                B = after[-1]
                if B.idx - A.idx >= 5:
                    slope = (B.price - A.price) / (B.idx - A.idx)
                    line = A.price + slope * (np.arange(t + 2) - A.idx)
                    if (c[A.idx + 1:t + 1] <= line[A.idx + 1:t + 1] * 1.01).all():
                        lvl = float(line[t + 1])
                        out.append(("L6", lvl, lows_after[-1].price, C.BIG_VOL,
                                    f"切線 {f2(A.price)}→{f2(B.price)}，明天約 {lvl:.2f}"))

    # ---------- 空方 ----------
    # S1 盤頭確認：多頭上漲高檔，頭不再過高，明天跌破頸線（最近的底）→ 空頭確認
    if tr != BEAR and len(H) >= 2 and len(L) >= 1 and had_trend(a, t, BULL) and rise_from_low(a, t) >= 0.2:
        h0, h1 = H[-2], H[-1]
        neck = [p.price for p in L if p.idx > h0.idx]
        if h1.price <= h0.price * 1.03 and neck and not a.ma_up(20, t):
            lvl = min(neck)
            if c[t] > lvl:
                out.append(("S1", lvl, h1.price, C.BIG_VOL, f"頸線 {f2(lvl)}，頭 {f2(h0.price)}→{f2(h1.price)}"))

    # S2 彈後空下跌：空頭反彈，不過前高、不過月線，明天跌破 5 日線
    if tr == BEAR and st == 1 and c[t] > ma5 and H and a.ma_down(20, t):
        bounce_hi = float(h[gs:t + 1].max())
        if bounce_hi < H[-1].price and (c[gs:t + 1] <= a.ma[20][gs:t + 1]).all():
            out.append(("S2", None, bounce_hi, 0, f"反彈高點 {f2(bounce_hi)}，前高 {f2(H[-1].price)}"))

    # S3 跌破盤整下頸線／S6 假突破真下跌
    if tr == RANGE and bx and not a.ma_up(20, t) and (c[t] < ma60 or a.ma_down(60, t)):
        upper, lower, s = bx
        if lower < c[t] <= upper and not (had_trend(a, t, BULL) and rise_from_low(a, t) >= 0.2):
            fake = (c[max(s, t - 10):t + 1] > upper).any()
            key = "S6" if fake else "S3"
            out.append((key, lower, lower, C.BIG_VOL, f"下頸線 {f2(lower)}／上頸線 {f2(upper)}" + ("，曾假突破" if fake else "")))

    # S4 跌破一字底（均線糾結）／空頭中的 K 線橫盤
    if tw:
        n, hi, lo = tw
        out.append(("S4", lo, hi, C.BIG_VOL, f"一字橫盤 {n} 天，{f2(lo)}–{f2(hi)}"))
    elif fw and tr == BEAR:
        n, hi, lo = fw
        out.append(("S4", lo, hi, C.BIG_VOL, f"K線橫盤 {n} 天，{f2(lo)}–{f2(hi)}"))

    # S5 空頭紅K反彈 1～2 天後，黑K跌破紅K低點
    if tr == BEAR:
        reds = [k for k in (t, t - 1) if a.kbar(k)["red"] and a.c[k] > a.c[k - 1]]
        if reds and t - 2 >= 0 and not (a.kbar(t - 2)["red"] and a.c[t - 2] > a.c[t - 3]):
            k = min(reds)
            if (c[k:t + 1] >= l[k]).all():
                out.append(("S5", float(l[k]), float(h[k:t + 1].max()), 0, f"反彈紅K低點 {f2(l[k])}"))

    return out


# ------------------------------------------------------------------
# 明天的觸發價
# ------------------------------------------------------------------
def trigger(a, t, side, level):
    """做多：明天收盤要站上 5 日線、突破今天高點、漲 2% 以上、過型態價位，取最高的。
    做空：反過來取最低的。"""
    ma5_need = float(a.c[t - 3:t + 1].mean())  # 明天收盤 >= 前 4 天收盤平均 ⇔ 站上明天的 5 日線
    if side > 0:
        p = max(a.h[t], ma5_need, a.c[t] * (1 + C.ENTRY_MIN_CHG), level or 0)
        return round_up(float(p))
    p = min(a.l[t], ma5_need, a.c[t] * (1 - C.ENTRY_MIN_CHG), level if level is not None else math.inf)
    return round_down(float(p))


# ------------------------------------------------------------------
# 戒律與守則
# ------------------------------------------------------------------
def _divergence(a, t, side):
    """量價背離：價格 3 天往同方向走，量卻縮。"""
    c, v = a.c, a.v
    price = c[t] > c[t - 3] if side > 0 else c[t] < c[t - 3]
    return price and v[t - 2:t + 1].mean() < v[t - 5:t - 2].mean()


def rules_long(a, t, key, trig):
    """回傳違反的戒律／守則清單（空的＝通過）。"""
    bad = []
    c, ma20 = a.c, a.ma[20][t]
    kb = a.kbar(t)
    if trig < ma20:
        bad.append("戒律1：盤底還沒突破月線")
    if a.up_streak(t) >= 2:
        bad.append("戒律2：明天是上漲第3根以上，勿追高")
    bias = c[t] / ma20 - 1
    spread = a.ma[5][t] / ma20 - 1
    if _divergence(a, t, 1) and a.k[t] >= C.KD_HIGH and bias >= C.BIAS_MAX and spread >= C.MA_SPREAD_MAX:
        bad.append("戒律3：量價背離＋KD高檔＋乖離過大＋均線開口大")
    above = [x for x in a.w_heads if x > trig]
    if above and min(above) / trig - 1 <= C.WEEKLY_PRESSURE:
        bad.append(f"戒律4：週線壓力 {f2(min(above))} 不到 5%")
    if not bad and (c[t - 10:t + 1] < a.ma[20][t - 10:t + 1]).any() and c[t] < ma20 and trig < ma20:
        bad.append("戒律5：回檔跌破月線，還沒站回")
    if key in ("L2", "L5") and a.trend[t] != BULL:
        bad.append("戒律6：回檔已跌破前低")
    if a.trend[t] == BEAR and key not in ("L1", "L4", "L6"):
        bad.append("戒律8：一般空頭的反彈")
    if a.high_zone[t] and c[t] / c[t - 5] - 1 >= 0.15 and kb["long_red"] and a.vol_ratio(t) >= C.BIG_VOL:
        bad.append("戒律9：連續急漲的大量長紅K高檔")
    # 短線守則 12：高檔爆量長上影線是壓力，離高點不到 5% 先等
    for k in range(max(1, t - 20), t + 1):
        if a.high_zone[k] and a.vol_ratio(k) >= C.HUGE_VOL and a.kbar(k)["upper_ratio"] >= 0.5:
            if a.h[k] > trig and a.h[k] / trig - 1 < C.SHADOW_PRESSURE:
                bad.append(f"守則12：上方 {f2(a.h[k])} 有爆量長上影線壓力")
                break
    # 短線守則 14：高檔連 2 日爆量，回檔連 3 黑，回後買上漲不進
    if key == "L2":
        st, gs = group(a, t)
        for k in range(max(1, gs - 10), gs + 1):
            if a.high_zone[k] and a.vol_ratio(k) >= C.HUGE_VOL and a.vol_ratio(k - 1) >= C.HUGE_VOL:
                blacks = [a.kbar(j)["black"] for j in range(k + 1, t + 1)]
                run = max((len(s) for s in "".join("b" if x else " " for x in blacks).split()), default=0)
                if run >= 3:
                    bad.append("守則14：高檔連2日爆量、回檔連3黑")
                    break
    return bad


def rules_short(a, t, key, trig):
    bad = []
    c, ma20 = a.c, a.ma[20][t]
    kb = a.kbar(t)
    if trig > ma20:
        bad.append("戒律1：盤頭還沒跌破月線")
    if a.down_streak(t) >= 2:
        bad.append("戒律2：明天是下跌第3根以上，勿殺低")
    bias = c[t] / ma20 - 1
    spread = a.ma[5][t] / ma20 - 1
    if _divergence(a, t, -1) and a.k[t] <= C.KD_LOW and bias <= -C.BIAS_MAX and spread <= -C.MA_SPREAD_MAX:
        bad.append("戒律3：量價背離＋KD低檔＋乖離過大＋均線開口大")
    below = [x for x in a.w_lows if x < trig]
    if below and 1 - max(below) / trig <= C.WEEKLY_PRESSURE:
        bad.append(f"戒律4：週線支撐 {f2(max(below))} 不到 5%")
    if not bad and (c[t - 10:t + 1] > a.ma[20][t - 10:t + 1]).any() and c[t] > ma20 and trig > ma20:
        bad.append("戒律5：反彈突破月線，還沒跌破")
    if key in ("S2", "S5") and a.trend[t] != BEAR:
        bad.append("戒律6：反彈已突破前高")
    if a.trend[t] == BULL and key not in ("S1", "S4"):
        bad.append("戒律8：一般多頭的回檔")
    if a.low_zone[t] and 1 - c[t] / c[t - 5] >= 0.15 and kb["long_black"] and a.vol_ratio(t) >= C.BIG_VOL:
        bad.append("戒律9：連續急跌的大量長黑K低檔")
    return bad


# ------------------------------------------------------------------
# 打分數（排序用，0～100）
# ------------------------------------------------------------------
def score(a, t, side, key, pos, rs, gap, vol_recent, vol_today):
    s = 50.0
    ma20 = a.ma[20][t]
    slope = a.ma[20][t] / a.ma[20][t - 5] - 1
    bias = a.c[t] / ma20 - 1
    if side > 0:
        s += {"初升段": 15, "主升段": 5}.get(pos["name"], 0)
        s += 10 if a.bull_aligned(t) else 0
        s += max(0.0, min(10.0, slope * 200))
        s -= max(0.0, min(15.0, (bias - 0.08) * 100))
        s += max(0.0, min(10.0, (rs - 50) / 5)) if rs is not None else 0
        s += 3 if a.osc[t] > a.osc[t - 1] else 0
        s += 2 if a.k[t] > a.k[t - 1] else 0
        tags = {x for x, _ in vol_recent}
        s += 5 if "換手量" in tags else 0
        s += 3 if "止跌量" in tags else 0
        s -= 8 if "出貨量" in vol_today and a.high_zone[t] else 0
        s += 3 if key == "L7" else 0
    else:
        s += {"初跌段": 15, "主跌段": 5}.get(pos["name"], 0)
        s += 10 if a.bear_aligned(t) else 0
        s += max(0.0, min(10.0, -slope * 200))
        s -= max(0.0, min(15.0, (-bias - 0.08) * 100))
        s += max(0.0, min(10.0, (50 - rs) / 5)) if rs is not None else 0
        s += 3 if a.osc[t] < a.osc[t - 1] else 0
        s += 2 if a.k[t] < a.k[t - 1] else 0
        tags = {x for x, _ in vol_recent}
        s -= 5 if "止跌量" in tags or "搶反彈量" in tags else 0
        s += 3 if key == "S6" else 0
    s -= max(0.0, min(5.0, gap * 100))
    return int(round(max(0.0, min(100.0, s))))


# ------------------------------------------------------------------
# 組合：一檔股票、一天 → 多方／空方各一筆
# ------------------------------------------------------------------
def _checks(a, t, side, key, pos, vol_today, vol_recent, bad):
    tr = a.trend[t]
    want = BULL if side > 0 else BEAR
    reversal = key in ("L1", "L6", "S1")
    trend_ok = tr == want or (reversal and tr != -want) or key in ("L3", "L4", "L7", "S3", "S4", "S6")
    aligned = a.bull_aligned(t) if side > 0 else a.bear_aligned(t)
    ma20_ok = a.ma_up(20, t) if side > 0 else a.ma_down(20, t)
    k_dir = "向上" if a.k[t] > a.k[t - 1] else "向下"
    osc_dir = "增加" if a.osc[t] > a.osc[t - 1] else "減少"
    tags = vol_today + [f"{x}（{-w}天前）" for x, w in vol_recent]
    return [
        {"step": "趨勢", "ok": bool(trend_ok), "text": TREND_NAME[tr] + ("（轉折中）" if reversal and tr != want else "")},
        {"step": "均線", "ok": bool(ma20_ok), "text": ("四線多排" if side > 0 else "四線空排") if aligned
            else ("月線上揚" if a.ma_up(20, t) else "月線下彎" if a.ma_down(20, t) else "月線走平")},
        {"step": "位階", "ok": not pos["late"], "text": f"{pos['name']}（第{pos['wave']}波，{pos['gain']:+.0%}）"},
        {"step": "成交量", "ok": True, "text": "、".join(tags) if tags else "量平"},
        {"step": "指標", "ok": bool(a.k[t] > a.k[t - 1]) if side > 0 else bool(a.k[t] < a.k[t - 1]),
         "text": f"K {a.k[t]:.0f} {k_dir}" + ("" if (a.k[t] > a.k[t - 1]) == (side > 0) else "（進場日要轉" + ("上" if side > 0 else "下") + "）")
                 + f"，MACD柱{osc_dir}"},
        {"step": "戒律", "ok": not bad, "text": "通過" if not bad else "；".join(bad)},
    ]


def evaluate(a, t, rs=None, vol_info=None):
    """回傳 {"long": dict|None, "short": dict|None, "excluded": [dict]}。"""
    from indicators import volume_tags

    if vol_info is None:
        vol_info = volume_tags(a.o, a.h, a.l, a.c, a.v, a.vbase, a.trend, t, a.high_zone, a.low_zone)
    vol_today, vol_recent = vol_info
    setups = find_setups(a, t)
    setups.sort(key=lambda s: ORDER.index(s[0]))
    res = {"long": None, "short": None, "excluded": []}
    for key, level, stop, need_vol, note in setups:
        side = 1 if key.startswith("L") else -1
        slot = "long" if side > 0 else "short"
        if res[slot] is not None:
            continue
        trig = trigger(a, t, side, level)
        gap = trig / a.c[t] - 1 if side > 0 else 1 - trig / a.c[t]
        if gap > C.MAX_GAP:
            continue
        pos = position(a.c, a.h, a.l, a.pivots20, t, side)
        bad = rules_long(a, t, key, trig) if side > 0 else rules_short(a, t, key, trig)
        if pos["late"]:
            bad.append(f"位階：{pos['name']}不做")
        if rs is not None and side > 0 and rs < C.RS_MIN_LONG:
            bad.append(f"強度 {rs} 太弱，做多要強勢股")
        if rs is not None and side < 0 and rs > C.RS_MAX_SHORT:
            bad.append(f"強度 {rs} 太強，做空要弱勢股")
        fixed = trig * (1 - C.STOP_PCT) if side > 0 else trig * (1 + C.STOP_PCT)
        item = {
            "side": slot,
            "key": key,
            "title": TITLES[key],
            "note": note,
            "close": round(float(a.c[t]), 2),
            "trigger": trig,
            "gap_pct": round(float(gap) * 100, 2),
            "need_vol": need_vol,
            "vol5": int(round(float(a.v[t - 4:t + 1].mean()))),   # 明天的基本量
            "ref": {  # 盤中檢查要用的數字
                "k": round(float(a.k[t]), 2),
                "low_n1": round(float(a.l[t - C.KD_N + 2:t + 1].min()), 2),
                "high_n1": round(float(a.h[t - C.KD_N + 2:t + 1].max()), 2),
                "ma20": round(float(a.ma[20][t]), 2),
            },
            "stop": {
                "fixed": round_down(fixed) if side > 0 else round_up(fixed),
                "trend": round(float(stop), 2) if stop else None,
            },
            "position": pos["name"],
            "trend": TREND_NAME[a.trend[t]],
            "rs": rs,
            "score": score(a, t, side, key, pos, rs, gap, vol_recent, vol_today),
            "checks": _checks(a, t, side, key, pos, vol_today, vol_recent, bad),
        }
        if bad:
            item["why"] = bad
            res["excluded"].append(item)
        else:
            res[slot] = item
    return res


def entry_bar_ok(a, t, side, trig, need_vol):
    """第 t 天這根 K 是不是合格的進場 K（收盤確認）。回傳 (是否合格, 不合格原因)。"""
    kb = a.kbar(t)
    why = []
    if side > 0:
        if a.c[t] < trig:
            why.append("收盤沒到觸發價")
        if not kb["solid_red"]:
            why.append("不是漲 2% 以上的實體紅K")
        if kb["up_black"]:
            why.append("價漲的黑K（戒律10）")
        if kb["upper_ratio"] > C.MAX_SHADOW:
            why.append("上影線超過一半")
        if a.k[t] <= a.k[t - 1]:
            why.append("K值沒有向上")
    else:
        if a.c[t] > trig:
            why.append("收盤沒到觸發價")
        if not kb["solid_black"]:
            why.append("不是跌 2% 以上的實體黑K")
        if kb["down_red"]:
            why.append("價跌的紅K（戒律10）")
        if kb["lower_ratio"] > C.MAX_SHADOW:
            why.append("下影線超過一半")
        if a.k[t] >= a.k[t - 1]:
            why.append("K值沒有向下")
    if need_vol and a.vol_ratio(t) < need_vol:
        why.append(f"量 {a.vol_ratio(t):.1f} 倍，不到 {need_vol} 倍")
    return not why, why


def confirmed_today(a, rs=None):
    """昨天收盤後的預備名單裡，今天收盤已經走出合格進場 K 的。"""
    t = a.t
    if t < 2:
        return []
    out = []
    y = evaluate(a, t - 1, rs)
    for slot in ("long", "short"):
        it = y[slot]
        if not it:
            continue
        side = 1 if slot == "long" else -1
        ok, _ = entry_bar_ok(a, t, side, it["trigger"], it["need_vol"])
        if ok:
            stop_k = float(a.l[t]) if side > 0 else float(a.h[t])
            fixed = a.c[t] * (1 - C.STOP_PCT) if side > 0 else a.c[t] * (1 + C.STOP_PCT)
            out.append({**it, "close": round(float(a.c[t]), 2), "entry": round(float(a.c[t]), 2),
                        "chg_pct": round((a.c[t] / a.c[t - 1] - 1) * 100, 2),
                        "stop": {"fixed": round_down(fixed) if side > 0 else round_up(fixed),
                                 "kline": round(stop_k, 2), "trend": it["stop"]["trend"]}})
    return out
