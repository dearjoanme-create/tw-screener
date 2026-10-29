"""三種型態的判斷。每個函式吃一檔股票的日 K（依日期排序），
符合就回傳 dict（含明天的觸發價），不符合回傳 None。"""
import numpy as np

import config as C


def pivots(values, window, kind):
    """找波段高點（kind='high'）或低點（'low'）。左右各 window 根都比它低/高才算。
    最後 window 根還沒被確認，不列入。"""
    out = []
    n = len(values)
    for i in range(window, n - window):
        left = values[i - window:i]
        right = values[i + 1:i + window + 1]
        v = values[i]
        # 左邊嚴格比它低、右邊不高於它（同價取第一根）
        if kind == "high" and v > left.max() and v >= right.max():
            out.append(i)
        if kind == "low" and v < left.min() and v <= right.min():
            out.append(i)
    return out


def _ma(a, n):
    return a[-n:].mean()


# ------------------------------------------------------------------
# 型態 1：多頭格局回檔，明天站回 5MA 且過前一天高點
# ------------------------------------------------------------------
def pullback(df):
    c, h, l, v = (df[k].to_numpy(float) for k in ("close", "high", "low", "volume"))
    if len(c) < 60 + C.PB_MA60_RISING_DAYS:
        return None
    ma5, ma20, ma60 = _ma(c, 5), _ma(c, 20), _ma(c, 60)
    ma60_prev = c[-60 - C.PB_MA60_RISING_DAYS:-C.PB_MA60_RISING_DAYS].mean()

    # 多頭排列：收盤 > 20MA > 60MA，60MA 上彎
    if not (c[-1] > ma20 > ma60 and ma60 > ma60_prev):
        return None

    # 頭頭高、底底高（最近兩個波段高點、低點都墊高）
    lb = C.PB_LOOKBACK
    ph = pivots(h[-lb:], C.PB_PIVOT_WINDOW, "high")
    pl = pivots(l[-lb:], C.PB_PIVOT_WINDOW, "low")
    if len(ph) < 2 or len(pl) < 2:
        return None
    hh = h[-lb:][ph[-1]] > h[-lb:][ph[-2]]
    hl = l[-lb:][pl[-1]] > l[-lb:][pl[-2]]
    if not (hh and hl):
        return None

    # 前一天回到 5MA 附近或跌破，且量縮
    if c[-1] > ma5 * (1 + C.PB_MA5_TOLERANCE):
        return None
    if v[-1] >= _ma(v, 5):
        return None

    # 明天要「站回 5MA」：明天的 5MA = (前 4 天收盤 + 明天價) / 5
    # 價格 >= 明天 5MA  ⇔  價格 >= 前 4 天收盤平均
    reclaim_ma5 = c[-4:].mean()
    trigger = max(reclaim_ma5, h[-1])
    return {
        "trigger": round(float(trigger), 2),
        "vol_ratio": 0,  # 這個型態不要求量
        "note": f"5MA {ma5:.1f}／20MA {ma20:.1f}，量縮回檔",
        "score": trigger / c[-1] - 1,  # 離觸發越近越前面
    }


# ------------------------------------------------------------------
# 型態 2：突破下降切線
# ------------------------------------------------------------------
def trendline(df):
    c, h, v = (df[k].to_numpy(float) for k in ("close", "high", "volume"))
    lb = C.TL_LOOKBACK
    if len(c) < lb:
        return None
    hh, cc = h[-lb:], c[-lb:]
    n = len(hh)
    ph = pivots(hh, C.TL_PIVOT_WINDOW, "high")
    if len(ph) < 2:
        return None

    # A = 區間內最高的波段高點；B = A 之後、比 A 低、離今天最近、且切線沒被破的高點
    a = max(ph, key=lambda i: hh[i])
    best = None
    for b in reversed([i for i in ph if i > a]):
        if b - a < C.TL_MIN_GAP_BARS or hh[b] > hh[a] * (1 - C.TL_MIN_DROP):
            continue
        slope = (hh[b] - hh[a]) / (b - a)
        line = hh[a] + slope * (np.arange(n) - a)
        # 從 A 到今天，收盤都沒有明顯站上切線
        if np.any(cc[a + 1:] > line[a + 1:] * (1 + C.TL_BREAK_TOLERANCE)):
            continue
        best = (b, slope)
        break
    if best is None:
        return None
    b, slope = best

    tomorrow = hh[a] + slope * (n - a)  # 切線延伸到明天的位置
    if tomorrow <= 0 or not (tomorrow * (1 - C.TL_MAX_BELOW) <= c[-1] < tomorrow):
        return None
    return {
        "trigger": round(float(tomorrow), 2),
        "vol_ratio": C.TL_VOL_RATIO,
        "note": f"切線 {hh[a]:.1f}→{hh[b]:.1f}（{n - a} 天前起）",
        "score": tomorrow / c[-1] - 1,
    }


# ------------------------------------------------------------------
# 型態 3：突破盤整區間
# ------------------------------------------------------------------
def consolidation(df):
    c, h, l = (df[k].to_numpy(float) for k in ("close", "high", "low"))
    if len(c) < C.RG_MAX_DAYS + 1:
        return None
    found = None
    # 從最長的天數往短找，取符合條件的最長盤整
    for n in range(C.RG_MAX_DAYS, C.RG_MIN_DAYS - 1, -1):
        hi, lo = h[-n:].max(), l[-n:].min()
        if hi / lo - 1 <= C.RG_MAX_WIDTH:
            found = (n, hi, lo)
            break
    if not found:
        return None
    n, hi, lo = found
    if c[-1] < hi * (1 - C.RG_NEAR_TOP):
        return None
    width = hi / lo - 1
    return {
        "trigger": round(float(hi), 2),
        "vol_ratio": C.RG_VOL_RATIO,
        "note": f"盤整 {n} 天，區間 {lo:.1f}–{hi:.1f}（{width:.1%}）",
        "score": width - n / 1000,  # 區間越窄、越久越前面
    }


PATTERNS = {
    "pullback": ("多頭回檔再上漲", pullback),
    "trendline": ("突破下降切線", trendline),
    "range": ("突破盤整區間", consolidation),
}
