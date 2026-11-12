"""測試用的人工走勢。legs = [(天數, 這段總漲跌幅), ...]"""
import numpy as np
import pandas as pd


def build(legs, start=50.0, spread=0.004, vol=2000.0, seed=0, noise=0.0, end="2026-11-04"):
    rng = np.random.default_rng(seed)
    c, p = [], start
    for n, total in legs:
        step = (1 + total) ** (1 / n)
        for _ in range(n):
            p *= step * (1 + rng.normal(0, noise)) if noise else step
            c.append(p)
    c = np.array(c)
    o = np.r_[start, c[:-1]]
    h = np.maximum(o, c) * (1 + spread)
    l = np.minimum(o, c) * (1 - spread)
    v = np.full(len(c), vol)
    return pd.DataFrame({"date": pd.bdate_range(end=end, periods=len(c)),
                         "open": o, "high": h, "low": l, "close": c, "volume": v})


def add_bar(df, o, h, l, c, v):
    d = df["date"].iloc[-1] + pd.offsets.BDay(1)
    row = pd.DataFrame({"date": [d], "open": [o], "high": [h], "low": [l], "close": [c], "volume": [v]})
    return pd.concat([df, row], ignore_index=True)


def flat(n, seed=0, amp=0.01):
    """n 天小幅震盪（總漲跌 0）。"""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        out.append((1, float(rng.uniform(-amp, amp))))
    return out


def uptrend_pullback():
    """多頭：漲 8 天、回 4 天，逐波墊高；最後漲一波後回檔 3 天（收在 5 日線下、月線上）。"""
    return build(flat(70) + [(8, 0.12), (4, -0.04)] * 4 + [(8, 0.12), (3, -0.035)])


def downtrend_rebound():
    """空頭：跌 8 天、彈 4 天，逐波走低；最後跌一波後反彈 3 天（收在 5 日線上、月線下）。"""
    return build(flat(70) + [(8, -0.12), (4, 0.04)] * 4 + [(8, -0.12), (3, 0.035)], start=120)


def bull_box():
    """多頭漲一波後進入盤整：頭 頭（略低），底 底（略高），收在上頸線下方。"""
    return build(flat(60) + [(10, 0.25), (6, -0.08), (6, 0.07), (6, -0.06), (4, 0.045), (2, -0.004)])


def bottom_base():
    """空頭跌深後打底：底不再破，收在頸線下方。"""
    return build(flat(40) + [(15, -0.20), (5, 0.05), (10, -0.15), (5, 0.06), (6, -0.06), (5, 0.06),
                             (4, -0.03), (3, 0.03), (1, -0.004)], start=100)


def top_formation():
    """多頭漲高後做頭：頭不再過高，收在頸線上方。"""
    return build(flat(40) + [(15, 0.25), (5, -0.05), (10, 0.15), (5, -0.06), (6, 0.05), (5, -0.05),
                             (4, 0.025), (2, -0.01), (1, 0.003)], start=50)


def tangle():
    """均線糾結的一字底：長期橫盤。"""
    return build(flat(60, amp=0.03) + flat(50, seed=5, amp=0.004))


def big_black():
    """多頭上漲中出一根大量長黑，隔天小紅。"""
    df = build(flat(70) + [(8, 0.12), (4, -0.04)] * 3 + [(10, 0.15)])
    c = df["close"].iloc[-1]
    df = add_bar(df, c * 1.005, c * 1.01, c * 0.955, c * 0.96, 5000)
    c2 = c * 0.96
    return add_bar(df, c2, c2 * 1.012, c2 * 0.995, c2 * 1.005, 2200)


def mirror(df, k=5000.0):
    """上下翻轉（價格取倒數）：多頭圖變空頭圖，用來測空方規則。"""
    out = df.copy()
    out["open"], out["close"] = k / df["open"], k / df["close"]
    out["high"], out["low"] = k / df["low"], k / df["high"]
    return out
