"""用人工做出來的走勢測試選股規則。執行：python -m tests.test_patterns"""
import datetime as dt
import json
from unittest import mock

import numpy as np
import pandas as pd

import data
import market
import patterns as P
import scan
from indicators import BEAR, BULL, RANGE, analyze, kd, next_k, turning_waves, sma
from tests.charts import (add_bar, big_black, bottom_base, build, bull_box, downtrend_rebound, flat, mirror,
                          tangle, uptrend_pullback)


def test_turning_waves_follow_book():
    # 取點原則：跌破 5 日線取正價群（含當天）最高點；突破 5 日線取負價群（含當天）最低點
    df = build(flat(20) + [(6, 0.10), (4, -0.06), (6, 0.10), (4, -0.06)])
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    ma5 = sma(c, 5)
    piv, _, _ = turning_waves(h, l, c, ma5)
    kinds = [p.kind for p in piv]
    assert all(a != b for a, b in zip(kinds, kinds[1:])), "頭、底要交錯出現"
    for p in piv:
        above = c[:p.conf] > ma5[:p.conf]
        # 這一群從上一次穿越開始，到確認那天為止
        start = max([q.conf for q in piv if q.conf < p.conf], default=4)
        seg = slice(start, p.conf + 1)
        if p.kind == "H":
            assert p.price == h[seg].max() and c[p.conf] < ma5[p.conf]
        else:
            assert p.price == l[seg].min() and c[p.conf] > ma5[p.conf]
        assert above is not None
    print("轉折波取點 OK：", [(p.kind, round(p.price, 2)) for p in piv[-4:]])


def test_trend_six_words():
    assert analyze(uptrend_pullback()).trend[-1] == BULL      # 頭頭高、底底高
    assert analyze(downtrend_rebound()).trend[-1] == BEAR     # 頭頭低、底底低
    assert analyze(bull_box()).trend[-1] == RANGE             # 頭頭低、底底高（收斂）＝盤整
    print("趨勢判定 OK")


def test_kd_and_next_k():
    df = uptrend_pullback()
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    k, d = kd(h, l, c)
    assert 0 <= k.min() and k.max() <= 100
    # 盤中算出來的 K 值，要跟把那根 K 棒加進去重算的結果一樣
    o2, h2, l2, c2 = c[-1], c[-1] * 1.04, c[-1] * 0.995, c[-1] * 1.035
    full = add_bar(df, o2, h2, l2, c2, 2600)
    k_full, _ = kd(*(full[x].to_numpy(float) for x in ("high", "low", "close")))
    k2 = next_k(k[-1], l[-8:].min(), h[-8:].max(), c2, l2, h2)
    assert abs(k2 - k_full[-1]) < 1e-9, (k2, k_full[-1])
    print("KD(9,3) 與盤中 K 值推算 OK")


def test_long_setups():
    cases = {
        "L2": (uptrend_pullback(), "回後買上漲"),
        "L3": (bull_box(), "突破盤整上頸線"),
        "L1": (bottom_base(), "打底完成突破"),
        "L5": (big_black(), "突破大量黑K"),
        "L4": (tangle(), "一字底突破"),
    }
    for key, (df, name) in cases.items():
        a = analyze(df)
        got = [s[0] for s in P.find_setups(a, a.t)]
        assert key in got, (name, got)
    a = analyze(uptrend_pullback())
    r = P.evaluate(a, a.t, rs=80)["long"]
    assert r and r["key"] == "L2"
    # 觸發價：站上明天 5 日線、突破今天高點、漲 2% 以上，取最高，並對齊升降單位
    t = a.t
    need = max(a.h[t], a.c[t - 3:t + 1].mean(), a.c[t] * 1.02)
    assert r["trigger"] >= need and r["trigger"] - need < P.tick(need) + 1e-9
    assert r["stop"]["fixed"] <= r["trigger"] * 0.95 and r["stop"]["trend"] > 0
    print("多方進場位置 OK：", r["title"], r["trigger"], r["note"])


def test_short_setups_on_mirrored_charts():
    cases = {"S2": uptrend_pullback(), "S3": bull_box(), "S1": bottom_base(), "S5": big_black()}
    for key, df in cases.items():
        a = analyze(mirror(df))
        r = P.evaluate(a, a.t, rs=30)
        got = [s[0] for s in P.find_setups(a, a.t)]
        assert key in got, (key, got)
        assert r["short"] is not None, (key, r["excluded"])
        assert r["short"]["trigger"] < r["short"]["close"]
    print("空方進場位置 OK（多方圖上下翻轉）")


def test_rules():
    # 已經連漲 2 天，明天是第 3 根 → 勿追高
    df = bottom_base()
    c = df["close"].iloc[-1]
    df = add_bar(df, c, c * 1.012, c * 0.998, c * 1.01, 2000)
    df = add_bar(df, c * 1.01, c * 1.022, c * 1.008, c * 1.02, 2000)
    a = analyze(df)
    bad = P.rules_long(a, a.t, "L1", P.trigger(a, a.t, 1, None))
    assert any("上漲第3根" in x for x in bad), bad
    # 觸發價在月線下 → 盤底還沒突破月線
    a = analyze(uptrend_pullback())
    assert any("盤底還沒突破月線" in x for x in P.rules_long(a, a.t, "L2", a.ma[20][a.t] * 0.95))
    # 上方有週線壓力
    a.w_heads = [a.c[a.t] * 1.06]
    trig = P.trigger(a, a.t, 1, None)
    assert any("週線壓力" in x for x in P.rules_long(a, a.t, "L2", trig))
    # 空頭的反彈不做多
    a = analyze(downtrend_rebound())
    assert any("空頭的反彈" in x for x in P.rules_long(a, a.t, "L3", a.c[a.t] * 1.03))
    # 多頭的回檔不做空
    a = analyze(uptrend_pullback())
    assert any("多頭的回檔" in x for x in P.rules_short(a, a.t, "S2", a.c[a.t] * 0.97))
    # 強度：做多要強勢股
    a = analyze(uptrend_pullback())
    res = P.evaluate(a, a.t, rs=20)
    assert res["long"] is None and any("強度" in w for e in res["excluded"] for w in e["why"])
    print("排除條件 OK")


def test_entry_bar_and_confirmed_today():
    df = uptrend_pullback()
    a = analyze(df)
    y = P.evaluate(a, a.t, rs=80)["long"]
    c = df["close"].iloc[-1]
    close = max(y["trigger"], c * 1.03)
    good = add_bar(df, c * 1.001, close * 1.003, c * 0.998, close, 2600)
    conf = P.confirmed_today(analyze(good), rs=80)
    assert conf and conf[0]["key"] == "L2" and conf[0]["entry"] == round(close, 2), conf
    # 價漲的黑K不進場（開高走低）
    bad = add_bar(df, close * 1.01, close * 1.02, c * 0.999, close, 2600)
    ok, why = P.entry_bar_ok(analyze(bad), len(bad) - 1, 1, y["trigger"], y["need_vol"])
    assert not ok and any("黑K" in w for w in why), why
    # 上影線超過一半
    shadow = add_bar(df, c * 1.001, close * 1.06, c * 0.998, close, 2600)
    ok, why = P.entry_bar_ok(analyze(shadow), len(shadow) - 1, 1, y["trigger"], y["need_vol"])
    assert not ok and any("上影線" in w for w in why), why
    print("進場 K 判斷、今日已確認 OK")


def test_market_status():
    idx = uptrend_pullback().drop(columns="volume")
    m = market.status(idx)
    assert m["trend"] == "多頭" and m["allow_long"] and not m["allow_short"], m
    m = market.status(mirror(idx.assign(volume=1)).drop(columns="volume"))
    assert m["trend"] == "空頭" and m["allow_short"] and not m["allow_long"], m
    print("大盤狀態 OK：", m["label"], m["capital"])


def test_index_parser():
    fake = {"stat": "OK", "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
            "data": [["115/11/03", "27,001.10", "27,200.50", "26,900.00", "27,150.25"],
                     ["115/11/04", "27,150.00", "27,300.00", "27,000.00", "27,010.00"]]}
    with mock.patch.object(data.requests, "get", return_value=mock.Mock(json=lambda: fake)):
        rows = data.fetch_index_month(dt.date(2026, 11, 1))
    assert rows[0][0] == dt.date(2026, 11, 3) and rows[0][4] == 27150.25 and len(rows) == 2
    print("加權指數解析 OK")


def test_parser():
    fields = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"]
    rows = [
        ["2330", "台積電", "30,123,456", "1", "1", "1,010.00", "1,025.00", "1,005.00", "1,020.00"],
        ["0050", "元大台灣50", "1,000,000", "1", "1", "100", "101", "99", "100"],
        ["1234", "沒成交", "0", "0", "0", "--", "--", "--", "--"],
    ]
    out = data._rows_from_table(fields, rows, "2026-10-28", "tse")
    assert len(out) == 1 and out[0][1] == "2330" and out[0][7] == 1020.0 and abs(out[0][8] - 30123.456) < 1e-6
    print("行情解析 OK")


def test_random_charts_do_not_crash():
    rng = np.random.default_rng(0)
    n_long = n_short = 0
    for s in range(150):
        r = rng.normal(0, 0.02, 320) + np.repeat(rng.normal(0, 0.004, 9), 40)[:320]
        df = build([(1, x) for x in r], seed=s)
        df["volume"] = 2000 * np.exp(rng.normal(0, 0.3, 320))
        a = analyze(df)
        res = P.evaluate(a, a.t, rs=int(rng.integers(0, 100)))
        n_long += res["long"] is not None
        n_short += res["short"] is not None
        P.confirmed_today(a, 50)
        json.dumps(res, ensure_ascii=False)  # 要能存成網頁資料
    print(f"150 檔隨機走勢：多方 {n_long}、空方 {n_short}")


def test_screen_end_to_end():
    frames = []
    for code, df in [("1101", uptrend_pullback()), ("2201", mirror(uptrend_pullback())),
                     ("3301", bull_box()), ("4401", bottom_base())]:
        df = df.copy()
        df["code"], df["name"], df["market"] = code, f"測試{code}", "tse"
        frames.append(df)
    all_df = pd.concat(frames)
    with mock.patch.object(scan, "rs_rank", return_value={"1101": 90, "2201": 10, "3301": 80, "4401": 70}):
        res = scan.screen(all_df, all_df["date"].max())
    assert any(r["code"] == "1101" and r["key"] == "L2" for r in res["long"]), res["long"]
    assert any(r["code"] == "2201" for r in res["short"]), res["short"]
    idx = uptrend_pullback().drop(columns="volume")
    mkt = market.status(idx)
    msg = scan.message(res, mkt, all_df["date"].max())
    print(msg)
    assert "多方預備" in msg and "空方預備" not in msg  # 大盤在月線上只推多方
    json.dumps(res, ensure_ascii=False)


def test_shortable():
    # 證交所格式：融資、融券各有一欄「次一營業日限額」，最後是註記
    f = ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
         "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"]
    rows = [["2330", "台積電"] + ["1"] * 5 + ["1,000"] + ["1"] * 5 + ["1,000", "0", ""],
            ["1101", "台泥"] + ["1"] * 5 + ["1,000"] + ["1"] * 5 + ["1,000", "0", "X"],   # 停止融券
            ["2002", "中鋼"] + ["1"] * 5 + ["1,000"] + ["1"] * 5 + ["0", "0", ""],        # 融券限額 0
            ["0050", "ETF"] + ["1"] * 5 + ["1,000"] + ["1"] * 5 + ["1,000", "0", ""]]
    got = {k: v[0] for k, v in data._margin_table(f, rows).items()}
    assert got == {"2330": True, "1101": False, "2002": False}, got
    assert data._margin_table(f, rows)["2330"][1:] == (1.0, 1.0)  # 融資、融券今日餘額
    # 櫃買格式：券限額、備註
    f2 = ["代號", "名稱", "前資餘額", "資買", "資賣", "現償", "資餘額", "資屬證金", "資使用率", "資限額",
          "前券餘額", "券賣", "券買", "券償", "券餘額", "券屬證金", "券使用率", "券限額", "資券相抵", "備註"]
    rows2 = [["6488", "環球晶"] + ["1"] * 15 + ["500", "0", ""], ["5347", "世界"] + ["1"] * 15 + ["0", "0", ""]]
    assert {k: v[0] for k, v in data._margin_table(f2, rows2).items()} == {"6488": True, "5347": False}

    # 選股：不能融券的空方移到「被排除」；抓不到資料（None）就不過濾
    df = mirror(uptrend_pullback()).copy()
    df["code"], df["name"], df["market"] = "2201", "測試", "tse"
    last = df["date"].max()
    with mock.patch.object(scan, "rs_rank", return_value={"2201": 10}):
        res = scan.screen(df, last, {"tse": {"2201": False}, "otc": None})
        assert not res["short"] and any(scan.NO_SHORT in e["why"] for e in res["excluded"])
        res = scan.screen(df, last, {"tse": None, "otc": None})
        assert res["short"]
        msg = scan.message(res, market.status(mirror(uptrend_pullback()).drop(columns="volume")), last,
                           {"tse": None, "otc": None})
        assert "融券資料抓不到" in msg
    print("可融券過濾 OK")


def test_chips():
    # 證交所三大法人（單位：股 → 張）
    f = ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
         "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數", "投信買進股數", "投信賣出股數", "投信買賣超股數",
         "自營商買賣超股數", "三大法人買賣超股數"]
    rows = [["2330", "台積電", "1", "1", "1,500,000", "0", "1", "1", "200,000", "-50,000", "1,650,000"]]
    assert data._insti_table(f, rows)["2330"] == (1500.0, 200.0, -50.0, 1650.0)
    # 籌碼摘要與可融券
    d = pd.bdate_range(end="2026-11-06", periods=5)
    ch = pd.DataFrame([[x, "2330", 100, 10, 0, 110 if i else -5, 1000 + i * 50, 10, 1] for i, x in enumerate(d)]
                      + [[d[-1], "__tse__", None, None, None, None, None, None, 1]], columns=data.CHIPS_COLS)
    s = data.chips_summary(ch)["2330"]
    assert s["streak"] == 4 and s["total"] == 435 and abs(s["margin_chg"] - 0.2) < 1e-9, s
    ok = data.shortable_from(ch, {"2330": "tse", "1101": "tse", "6488": "otc"})
    assert ok["tse"] == {"2330": True, "1101": False} and ok["otc"] is None, ok
    print("籌碼解析 OK")


def test_holdings_rules():
    import holdings as HD
    df = uptrend_pullback()
    a = analyze(df)
    t = a.t
    entry_day = df["date"].iloc[t - 25].strftime("%Y-%m-%d")
    # 進場後漲超過 10%，今天收盤在 5 日線下 → 停利
    r = HD.evaluate(a, {"code": "1101", "side": "long", "price": a.c[t] / 1.15, "date": entry_day, "mode": "短"})
    assert r["level"] == "出場" and "收盤跌破 5 日線" in r["text"] and "停利" in r["text"], r
    # 漲幅未達 10%、跌破 5 日線 → 續抱
    r = HD.evaluate(a, {"code": "1101", "side": "long", "price": a.c[t] / 1.04, "date": entry_day, "mode": "短"})
    assert r["level"] in ("續抱", "警示") and any("跌破 5 日線續抱" in x for x in [r["text"]] + r["notes"]), r
    # 跌破停損
    r = HD.evaluate(a, {"code": "1101", "side": "long", "price": a.c[t] * 1.08, "date": entry_day, "mode": "短"})
    assert r["level"] == "出場" and "停損" in r["text"], r
    # 長線：沒跌破月線續抱
    r = HD.evaluate(a, {"code": "1101", "side": "long", "price": a.c[t] / 1.04, "date": entry_day, "mode": "長"})
    assert r["level"] in ("續抱", "警示") and "月線" in r["text"] + "".join(r["notes"]), r
    # 放空：多頭圖上放空 → 已轉多頭回補
    r = HD.evaluate(a, {"code": "1101", "side": "short", "price": a.c[t] * 1.02, "date": entry_day, "mode": "短"})
    assert r["level"] == "出場", r
    # 盤中檢查
    h = {"code": "1101", "side": "long", "price": 100, "mode": "短", "advice": {"stop": 95, "sum4": 460}}
    assert HD.live_check(h, 94)[0] == "出場"
    assert HD.live_check(h, 112)[0] == "出場"        # +12%，5 日線 =(460+112)/5=114.4 → 跌破停利
    assert HD.live_check(h, 96)[0] == "警示"
    assert HD.live_check(h, 103) is None
    lines = HD.push_section([r], {"1101": "台泥"})
    assert "持股檢查" in lines[0]
    print("持股規則 OK")


def test_screen_with_context():
    frames = []
    for code, df in [("1101", uptrend_pullback()), ("1102", bull_box()), ("1103", bottom_base()),
                     ("2201", mirror(uptrend_pullback()))]:
        df = df.copy()
        df["code"], df["name"], df["market"] = code, f"測試{code}", "tse"
        frames.append(df)
    all_df = pd.concat(frames)
    ind = {"1101": "水泥", "1102": "水泥", "1103": "水泥", "2201": "食品"}
    chips = {"1101": {"days": 5, "foreign": 800.0, "trust": 50.0, "total": 900.0, "streak": 3, "margin_chg": 0.02}}
    with mock.patch.object(scan, "rs_rank", return_value={"1101": 90, "1102": 80, "1103": 70, "2201": 10}), \
         mock.patch.object(scan, "sector_rank", return_value={"水泥": (1, 30), "食品": (30, 30)}):
        res = scan.screen(all_df, all_df["date"].max(), None, {"chips": chips, "industry": ind, "held": ["1101"]})
    r = next(x for x in res["long"] if x["code"] == "1101")
    steps = {c["step"]: c for c in r["checks"]}
    assert steps["籌碼"]["ok"] and steps["類股"]["ok"] and "水泥" in r["sector"], r["checks"]
    assert set(res["records"]) == {"1101", "1102", "1103", "2201"} and "1101" in res["analyses"]
    rec = res["records"]["1101"]
    assert rec["setups"] and rec["chips"].startswith("法人") and "第 1 強" in rec["sector"]
    json.dumps(res["records"], ensure_ascii=False)
    print("籌碼、類股、個股摘要 OK")


if __name__ == "__main__":
    test_chips()
    test_holdings_rules()
    test_screen_with_context()
    test_shortable()
    test_parser()
    test_index_parser()
    test_turning_waves_follow_book()
    test_trend_six_words()
    test_kd_and_next_k()
    test_long_setups()
    test_short_setups_on_mirrored_charts()
    test_rules()
    test_entry_bar_and_confirmed_today()
    test_market_status()
    test_random_charts_do_not_crash()
    test_screen_end_to_end()
    print("\n全部測試通過 ✅")
