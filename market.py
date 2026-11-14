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
        "allow_long": bool(above) or not C.MARKET_FILTER or C.LONG_BELOW_MARKET,
        "long_counter": bool(not above and C.MARKET_FILTER),  # 大盤在月線下還推的多方＝逆大盤短多
        "allow_short": (not above) or not C.MARKET_FILTER,
    }


def margin_status(mh, idx, mkt=None):
    """大盤融資狀態＋做多環境判讀（只標註、不擋）。
    mh：data/margin_hist.csv；idx：加權指數日K。回測：大盤月線上揚＋融資 5 日減少時做多最有利，
    月線下彎時做多勝率低；融資離 60 天低點的漲幅大於指數漲幅＝融資過熱。"""
    if mh is None or len(mh) < 25:
        return None
    m = mh.dropna(subset=["margin_amt"]).set_index("date")["margin_amt"]
    ix = idx.set_index("date")["close"]
    last = m.index[-1]
    chg5 = float(m.iloc[-1] - m.iloc[-6]) if len(m) > 5 else None
    m_up = float(m.iloc[-1] / m.iloc[-60:].min() - 1)
    i60 = ix[ix.index <= last].iloc[-60:]
    i_up = float(i60.iloc[-1] / i60.min() - 1) if len(i60) else 0.0
    hot = m_up > i_up
    maint = mh.dropna(subset=["maint"]).iloc[-1]["maint"] if mh["maint"].notna().any() else None
    up = (mkt or {}).get("ma20_dir") == "上揚"
    down = (mkt or {}).get("ma20_dir") == "下彎"
    if up and chg5 is not None and chg5 < 0:
        verdict, level = "做多有利（大盤向上、融資洗盤）", "good"
    elif down:
        verdict, level = "做多勝率較低（大盤月線下彎）", "bad"
    elif hot:
        verdict, level = "融資過熱，做多保守", "warn"
    elif up:
        verdict, level = "做多偏有利（融資沒有過熱）", "good"
    else:
        verdict, level = "中性", "neutral"
    return {"date": last.strftime("%Y-%m-%d"), "amt": round(float(m.iloc[-1]), 1),
            "chg5": None if chg5 is None else round(chg5, 1), "hot": bool(hot),
            "maint": None if maint is None else float(maint), "verdict": verdict, "level": level}
