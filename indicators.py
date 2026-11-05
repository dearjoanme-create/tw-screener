"""技術分析的基本計算：均線、KD、MACD、轉折波、趨勢、位階、成交量標籤、週線支撐壓力。
規則依據：朱家泓《活用技術分析寶典》。

每檔股票先用 analyze(df) 算一次，patterns.py 再拿結果判斷進場位置。"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config as C

BULL, RANGE, BEAR = 1, 0, -1
TREND_NAME = {BULL: "多頭", RANGE: "盤整", BEAR: "空頭"}


# ------------------------------------------------------------------
# 均線、KD、MACD
# ------------------------------------------------------------------
def sma(a, n):
    return pd.Series(a, dtype=float).rolling(n).mean().to_numpy()


def ema(a, n):
    return pd.Series(a, dtype=float).ewm(span=n, adjust=False).mean().to_numpy()


def kd(h, l, c, n=None, m=None):
    """台灣常用的 KD：K = 前K×(m-1)/m + RSV/m，D 同理，起始值 50。"""
    n = n or C.KD_N
    m = m or C.KD_M
    ll = pd.Series(l).rolling(n, min_periods=1).min().to_numpy()
    hh = pd.Series(h).rolling(n, min_periods=1).max().to_numpy()
    rng = hh - ll
    rsv = np.where(rng > 0, (c - ll) / np.where(rng > 0, rng, 1) * 100, 50.0)
    k = np.empty(len(c))
    d = np.empty(len(c))
    pk = pd_ = 50.0
    for i, r in enumerate(rsv):
        pk = pk * (m - 1) / m + r / m
        pd_ = pd_ * (m - 1) / m + pk / m
        k[i], d[i] = pk, pd_
    return k, d


def next_k(k_now, low_n1, high_n1, price, low_today=None, high_today=None):
    """明天（或盤中）收在 price 時的 K 值。low_n1/high_n1 = 前 n-1 天的最低/最高。"""
    lo = min(low_n1, price if low_today is None else min(low_today, price))
    hi = max(high_n1, price if high_today is None else max(high_today, price))
    rsv = 50.0 if hi <= lo else (price - lo) / (hi - lo) * 100
    m = C.KD_M
    return k_now * (m - 1) / m + rsv / m


def macd(c):
    dif = ema(c, C.MACD_FAST) - ema(c, C.MACD_SLOW)
    sig = ema(dif, C.MACD_SIGNAL)
    return dif, sig, dif - sig


# ------------------------------------------------------------------
# 轉折波（2-1 認識轉折波）
# ------------------------------------------------------------------
@dataclass
class Pivot:
    idx: int      # 高低點那根 K 棒
    kind: str     # "H" 頭、"L" 底
    price: float
    conf: int     # 在哪一根 K 棒確認（收盤跌破／突破均線那天）


def turning_waves(h, l, c, ma):
    """收盤在均線上方＝正價、下方＝負價。
    收盤跌破均線：正價群＋當天，取最高點（含上影線）當「頭」。
    收盤突破均線：負價群＋當天，取最低點（含下影線）當「底」。
    回傳 (轉折點清單, 目前狀態 1/-1, 目前這一群從第幾根開始)。"""
    pivots, state, start = [], None, None
    for i in range(len(c)):
        if np.isnan(ma[i]):
            continue
        s = 1 if c[i] > ma[i] else (-1 if c[i] < ma[i] else state)
        if state is None:
            state, start = s, i
            continue
        if s is not None and s != state:
            if state == 1:
                j = start + int(np.argmax(h[start:i + 1]))
                pivots.append(Pivot(j, "H", float(h[j]), i))
            else:
                j = start + int(np.argmin(l[start:i + 1]))
                pivots.append(Pivot(j, "L", float(l[j]), i))
            state, start = s, i
    return pivots, state, start


def trend_series(c, pivots):
    """每一天的趨勢：從當天收盤往左，和最近的頭、底比較（2-2、2-3 的 6 字口訣）。
    多頭：底底高，且頭頭高（或收盤已經過前頭），收盤沒跌破最近的底。
    空頭：頭頭低，且底底低（或收盤已經破前底），收盤沒站上最近的頭。
    其他都是盤整。"""
    n = len(c)
    out = np.zeros(n, dtype=int)
    heads, lows = [], []
    p = 0
    ps = sorted(pivots, key=lambda x: x.conf)
    for t in range(n):
        while p < len(ps) and ps[p].conf <= t:
            (heads if ps[p].kind == "H" else lows).append(ps[p].price)
            p += 1
        if len(heads) < 2 or len(lows) < 2:
            continue
        h1, h0, l1, l0 = heads[-1], heads[-2], lows[-1], lows[-2]
        bull = l1 > l0 and (h1 > h0 or c[t] > h1) and c[t] >= l1
        bear = h1 < h0 and (l1 < l0 or c[t] < l1) and c[t] <= h1
        if bull and not bear:
            out[t] = BULL
        elif bear and not bull:
            out[t] = BEAR
    return out


def confirmed(pivots, t, kind=None):
    """第 t 天為止已經確認的轉折點。"""
    return [p for p in pivots if p.conf <= t and (kind is None or p.kind == kind)]


# ------------------------------------------------------------------
# 位階：初升段／主升段／末升段（空頭：初跌／主跌／末跌）
# ------------------------------------------------------------------
def position(c, h, l, pivots, t, side, lookback=150):
    """從近 lookback 天的最低點（做空看最高點）起算，用月線長期轉折波數創新高的波段有幾個。"""
    s = max(0, t - lookback)
    if side > 0:
        base_i = s + int(np.argmin(l[s:t + 1]))
        base = float(l[base_i])
        gain = c[t] / base - 1 if base > 0 else 0
        heads = [p for p in confirmed(pivots, t, "H") if p.idx > base_i]
        waves, last = 0, base
        for p in heads:
            if p.price >= last * (1 + C.WAVE_MIN):
                waves += 1
                last = p.price
        wave = waves + 1  # 進場會落在第幾波
        late = bool((wave >= C.LATE_WAVE and gain >= C.LATE_WAVE_GAIN) or gain >= C.LATE_GAIN)
        name = "末升段" if late else ("初升段" if wave == 1 else "主升段")
        return {"name": name, "wave": wave, "gain": round(float(gain), 3), "base": round(base, 2), "late": late}
    base_i = s + int(np.argmax(h[s:t + 1]))
    base = float(h[base_i])
    drop = 1 - c[t] / base if base > 0 else 0
    lows = [p for p in confirmed(pivots, t, "L") if p.idx > base_i]
    waves, last = 0, base
    for p in lows:
        if p.price <= last / (1 + C.WAVE_MIN):
            waves += 1
            last = p.price
    wave = waves + 1
    late = bool((wave >= C.LATE_WAVE and drop >= C.LATE_WAVE_GAIN * 0.6) or drop >= C.LATE_GAIN / 2)
    name = "末跌段" if late else ("初跌段" if wave == 1 else "主跌段")
    return {"name": name, "wave": wave, "gain": round(float(-drop), 3), "base": round(base, 2), "late": late}


# ------------------------------------------------------------------
# K 線
# ------------------------------------------------------------------
def kbar(o, h, l, c, prev_c):
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    chg = c / prev_c - 1 if prev_c else 0.0
    return {
        "chg": chg,
        "red": c > o,
        "black": c < o,
        "body_pct": body / prev_c if prev_c else 0.0,
        "upper_ratio": upper / rng if rng > 0 else 0.0,
        "lower_ratio": lower / rng if rng > 0 else 0.0,
        "solid_red": c > o and chg >= C.ENTRY_MIN_CHG,
        "solid_black": c < o and chg <= -C.ENTRY_MIN_CHG,
        "long_red": c > o and body / prev_c >= C.LONG_BODY if prev_c else False,
        "long_black": c < o and body / prev_c >= C.LONG_BODY if prev_c else False,
        "up_black": chg > 0 and c < o,      # 價漲的黑K（做多戒律 10）
        "down_red": chg < 0 and c > o,      # 價跌的紅K（做空戒律 10）
    }


# ------------------------------------------------------------------
# 成交量（7-2 各種成交量的定義）
# ------------------------------------------------------------------
def volume_tags(o, h, l, c, v, base, trend, t, high_zone, low_zone):
    """回傳 (今天的量, 近期重要的量)。base[i] = 第 i 天之前的 5 日均量（基本量）。"""
    def ratio(i):
        return v[i] / base[i] if base[i] and base[i] > 0 else 0.0

    today = []
    r = ratio(t)
    chg = c[t] / c[t - 1] - 1
    if r >= C.HUGE_VOL:
        today.append("爆大量")
    elif r >= C.BIG_VOL and chg > 0:
        today.append("攻擊量")
    if r >= C.BIG_VOL and chg > 0:
        today.append("進貨量")
    if r >= C.BIG_VOL and chg < 0:
        today.append("出貨量")
    if chg < 0 and 0 < r <= C.DRY_VOL:
        today.append("量縮")

    recent = []
    for k in range(max(1, t - 20), t):
        rk = ratio(k)
        ck = c[k] / c[k - 1] - 1
        after = slice(k + 1, t + 1)
        when = k - t  # 負數：幾天前
        if k >= t - 10 and ck < 0 and 0 < rk <= C.DRY_VOL and l[after].min() >= l[k]:
            recent.append(("止跌量", when))
        if rk >= C.BIG_VOL and high_zone[k]:
            brk = [j for j in range(k + 1, t + 1) if c[j] > h[k]]
            if brk:
                j = brk[0]
                if j - k <= 3:
                    recent.append(("換手量", when))
                elif c[k + 1:j].min() < c[k]:
                    recent.append(("調節量", when))
        if k >= t - 10 and rk >= C.BIG_VOL and trend[k] == BEAR and low_zone[k]:
            if l[after].min() >= l[k] and any(c[j] > o[j] and c[j] > h[j - 1] for j in range(k + 1, t + 1)):
                recent.append(("搶反彈量", when))
    # 同一種量只留最近一次
    seen, out = set(), []
    for tag, when in sorted(recent, key=lambda x: -x[1]):
        if tag not in seen:
            seen.add(tag)
            out.append((tag, when))
    return today, out


# ------------------------------------------------------------------
# 週線支撐壓力（戒律 4）
# ------------------------------------------------------------------
def weekly_levels(df):
    """用週K的 5 週均線轉折波，回傳 (週線頭的價位, 週線底的價位)。"""
    w = (df.set_index("date")[["open", "high", "low", "close"]]
         .resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
         .dropna())
    if len(w) < 12:
        return [], []
    wh, wl, wc = (w[k].to_numpy(float) for k in ("high", "low", "close"))
    piv, _, _ = turning_waves(wh, wl, wc, sma(wc, 5))
    piv = [x for x in piv if x.idx >= len(w) - 52]  # 只看近一年
    return [p.price for p in piv if p.kind == "H"], [p.price for p in piv if p.kind == "L"]


# ------------------------------------------------------------------
# 一次算完
# ------------------------------------------------------------------
@dataclass
class Analysis:
    df: pd.DataFrame
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    ma: dict
    k: np.ndarray
    d: np.ndarray
    dif: np.ndarray
    osc: np.ndarray
    vbase: np.ndarray           # 第 i 天之前的 5 日均量
    pivots: list
    pivots20: list              # 月線（20 日）長期轉折波，算位階用
    wave_state: int             # 目前在均線上(1)／下(-1)
    wave_start: int             # 目前這一群從第幾根開始
    trend: np.ndarray
    high_zone: np.ndarray
    low_zone: np.ndarray
    w_heads: list = field(default_factory=list)
    w_lows: list = field(default_factory=list)

    @property
    def t(self):
        return len(self.c) - 1

    def heads(self, t=None):
        return confirmed(self.pivots, self.t if t is None else t, "H")

    def lows(self, t=None):
        return confirmed(self.pivots, self.t if t is None else t, "L")

    def ma_at(self, n, t=None):
        return float(self.ma[n][self.t if t is None else t])

    def ma_up(self, n, t=None, days=1):
        t = self.t if t is None else t
        return self.ma[n][t] > self.ma[n][t - days]

    def ma_down(self, n, t=None, days=1):
        t = self.t if t is None else t
        return self.ma[n][t] < self.ma[n][t - days]

    def bull_aligned(self, t=None):
        """四線多排：5 > 10 > 20 > 60，而且都向上。"""
        t = self.t if t is None else t
        m = [self.ma[n][t] for n in C.MA_LIST]
        return all(a > b for a, b in zip(m, m[1:])) and all(self.ma_up(n, t) for n in C.MA_LIST)

    def bear_aligned(self, t=None):
        t = self.t if t is None else t
        m = [self.ma[n][t] for n in C.MA_LIST]
        return all(a < b for a, b in zip(m, m[1:])) and all(self.ma_down(n, t) for n in C.MA_LIST)

    def tangled(self, t=None, lines=(5, 10, 20)):
        """均線糾結：幾條均線彼此差距很小。"""
        t = self.t if t is None else t
        m = [self.ma[n][t] for n in lines]
        return max(m) / min(m) - 1 <= C.TANGLE_MAX

    def kbar(self, t=None):
        t = self.t if t is None else t
        return kbar(self.o[t], self.h[t], self.l[t], self.c[t], self.c[t - 1])

    def vol_ratio(self, t=None):
        t = self.t if t is None else t
        b = self.vbase[t]
        return float(self.v[t] / b) if b and b > 0 else 0.0

    def up_streak(self, t=None):
        """到第 t 天為止連續收漲幾天。"""
        t = self.t if t is None else t
        n = 0
        while t - n >= 1 and self.c[t - n] > self.c[t - n - 1]:
            n += 1
        return n

    def down_streak(self, t=None):
        t = self.t if t is None else t
        n = 0
        while t - n >= 1 and self.c[t - n] < self.c[t - n - 1]:
            n += 1
        return n


def analyze(df, weekly=True):
    """df：一檔股票的日K（date, open, high, low, close, volume），依日期排序。"""
    df = df.sort_values("date").reset_index(drop=True)
    o, h, l, c, v = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    ma = {n: sma(c, n) for n in C.MA_LIST}
    k, d = kd(h, l, c)
    dif, _, osc = macd(c)
    vbase = np.r_[np.nan, sma(v, 5)[:-1]]
    pivots, ws, wst = turning_waves(h, l, c, ma[5])
    pivots20, _, _ = turning_waves(h, l, c, ma[20])
    trend = trend_series(c, pivots)
    hi60 = pd.Series(h).rolling(60, min_periods=20).max().to_numpy()
    lo60 = pd.Series(l).rolling(60, min_periods=20).min().to_numpy()
    high_zone = c >= hi60 * C.HIGH_ZONE
    low_zone = c <= lo60 * C.LOW_ZONE
    wh, wl = weekly_levels(df) if weekly else ([], [])
    return Analysis(df, o, h, l, c, v, ma, k, d, dif, osc, vbase, pivots, pivots20, ws, wst, trend,
                    high_zone, low_zone, wh, wl)
