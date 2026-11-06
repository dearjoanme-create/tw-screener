"""大盤狀態與建議資金水位，以及今天要推多方還是空方。"""
import config as C
from indicators import BEAR, BULL, TREND_NAME, analyze, position


def status(idx_df):
    """idx_df：加權指數日K（date, open, high, low, close）。"""
    if idx_df is None or len(idx_df) < 80:
        return None
    df = idx_df.copy()
    df["volume"] = 1.0
    a = analyze(df, weekly=False)
    t = a.t
    c, ma20, ma60 = a.c[t], a.ma[20][t], a.ma[60][t]
    tr = a.trend[t]
    pos = position(a.c, a.h, a.l, a.pivots20, t, 1 if tr != BEAR else -1)
    H = a.heads(t)

    if tr == BULL:
        just = (a.trend[max(0, t - 5):t] != BULL).any()
        if pos["late"]:
            label, cap = "多頭末升段", "5 成"
        elif just:
            label, cap = "多頭確認", "3 成"
        elif a.bull_aligned(t):
            label, cap = "中長多頭（四線多排）", "6～8 成"
        elif c > ma20 and a.ma_up(20, t):
            label, cap = "多頭，站上月線", "4～5 成"
        else:
            label, cap = "多頭", "3 成"
    elif tr == BEAR:
        if c < ma60 and a.ma_down(60, t) and a.bear_aligned(t):
            label, cap = "長期空頭", "2 成"
        elif a.bear_aligned(t):
            label, cap = "中期空頭（四線空排）", "3 成"
        else:
            label, cap = "空頭確認", "4 成"
    else:
        if len(H) >= 2 and H[-1].price < H[-2].price:
            label, cap = "盤整，出現頭頭低", "5 成以內"
        else:
            label, cap = "盤整", "4 成"

    above = c > ma20
    return {
        "date": a.df["date"].iloc[t].strftime("%Y-%m-%d"),
        "close": round(float(c), 2),
        "chg_pct": round(float(c / a.c[t - 1] - 1) * 100, 2),
        "ma20": round(float(ma20), 2),
        "ma20_dir": "上揚" if a.ma_up(20, t) else "下彎" if a.ma_down(20, t) else "走平",
        "trend": TREND_NAME[tr],
        "label": label,
        "capital": cap,
        "above_ma20": bool(above),
        "allow_long": bool(above) or not C.MARKET_FILTER,
        "allow_short": (not above) or not C.MARKET_FILTER,
    }
