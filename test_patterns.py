"""用人工做出來的走勢測試三種型態。執行：python -m tests.test_patterns"""
import numpy as np
import pandas as pd

import data
import scan
from patterns import consolidation, pullback, trendline


def make(closes, vols=None, spread=0.01):
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + spread)
    l = np.minimum(o, c) * (1 - spread)
    v = np.full(len(c), 2000.0) if vols is None else np.asarray(vols, float)
    return pd.DataFrame({"date": pd.bdate_range(end="2026-10-28", periods=len(c)),
                         "open": o, "high": h, "low": l, "close": c, "volume": v})


def uptrend_with_pullback():
    # 每段：漲 8 天、回 4 天，逐段墊高；最後 2 天回檔到 5MA 下方且量縮
    c, p = [], 50.0
    for _ in range(9):
        for _ in range(8):
            p *= 1.02; c.append(p)
        for _ in range(4):
            p *= 0.99; c.append(p)
    for _ in range(4):
        p *= 1.02; c.append(p)
    p *= 0.975; c.append(p)
    p *= 0.985; c.append(p)
    v = [2000] * (len(c) - 1) + [1200]
    return make(c, v)


def downtrend_near_line():
    # 下跌中反彈形成越來越低的高點；最後收在切線下方一點點
    rng = np.random.default_rng(1)
    c = list(np.linspace(80, 82, 20))
    seg_hi = 100.0
    for k in range(4):
        up = np.linspace(c[-1], seg_hi, 6)[1:]
        dn = np.linspace(seg_hi, seg_hi * 0.85, 8)[1:]
        c += list(up) + list(dn)
        seg_hi *= 0.93
    c += list(np.linspace(c[-1], c[-1] * 1.08, 5)[1:])
    c = np.array(c) * (1 + rng.normal(0, 0.002, len(c)))
    return make(c, spread=0.004)


def flat_range():
    rng = np.random.default_rng(2)
    c = list(np.linspace(40, 60, 40))
    base = 60
    c += list(base * (1 + rng.uniform(-0.035, 0.035, 30)))
    c += [base * 1.03]
    v = [3000] * 40 + list(np.linspace(2500, 1200, 31))  # 盤整期間量縮
    return make(c, v, spread=0.005)


def random_walk(seed):
    rng = np.random.default_rng(seed)
    return make(100 * np.cumprod(1 + rng.normal(0, 0.02, 150)))


def test_detectors():
    r = pullback(uptrend_with_pullback())
    print("多頭回檔：", r)
    assert r, "多頭回檔應該要抓到"

    r = trendline(downtrend_near_line())
    print("下降切線：", r)
    assert r, "下降切線應該要抓到"

    r = consolidation(flat_range())
    print("盤整區間：", r)
    assert r, "盤整區間應該要抓到"

    # 反向檢查：強勢上漲中（還沒回檔）不該被當成回檔
    up = make(50 * np.cumprod(np.full(120, 1.01)))
    assert pullback(up) is None
    # 持續上漲不是盤整
    assert consolidation(up) is None

    hits = {"pullback": 0, "trendline": 0, "range": 0}
    for s in range(500):
        df = random_walk(s)
        hits["pullback"] += bool(pullback(df))
        hits["trendline"] += bool(trendline(df))
        hits["range"] += bool(consolidation(df))
    print("500 檔隨機走勢的命中數：", hits)


def test_parser():
    fields = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"]
    rows = [
        ["2330", "台積電", "30,123,456", "1", "1", "1,010.00", "1,025.00", "1,005.00", "1,020.00"],
        ["0050", "元大台灣50", "1,000,000", "1", "1", "100", "101", "99", "100"],
        ["1234", "沒成交", "0", "0", "0", "--", "--", "--", "--"],
    ]
    out = data._rows_from_table(fields, rows, "2026-10-28", "tse")
    print("解析結果：", out)
    assert len(out) == 1 and out[0][1] == "2330" and out[0][7] == 1020.0 and abs(out[0][8] - 30123.456) < 1e-6


def test_screen_end_to_end():
    frames = []
    for code, df in [("1101", uptrend_with_pullback()), ("2201", downtrend_near_line()),
                     ("3301", flat_range()), ("4401", random_walk(9))]:
        df = df.copy()
        df["code"], df["name"], df["market"] = code, f"測試{code}", "tse"
        frames.append(df)
    all_df = pd.concat(frames)
    from unittest import mock
    # 只有 4 檔時強度排名沒意義，先關掉強度門檻測型態
    with mock.patch.dict(scan.config.RS_MIN, {"pullback": 0, "trendline": 0, "range": 0}):
        res = scan.screen(all_df, all_df["date"].max())
    print(scan.message(res, all_df["date"].max()))
    assert any(r["code"] == "1101" for r in res["pullback"])
    assert any(r["code"] == "2201" for r in res["trendline"])
    assert any(r["code"] == "3301" for r in res["range"])
    # 開啟強度門檻：下跌中的 2201 強度最低，應被盤整/回檔門檻擋下
    rs = scan.rs_rank(all_df.sort_values(["code", "date"]), all_df["date"].max())
    print("強度：", rs)
    assert rs["2201"] == min(rs.values())


if __name__ == "__main__":
    test_parser()
    test_detectors()
    test_screen_end_to_end()
    print("\n全部測試通過 ✅")
