"""資料抓取的穩定性測試。執行：python -m tests.test_robust"""
import datetime as dt
import json
import os
import tempfile
from unittest import mock

import numpy as np
import pandas as pd

import data
import line_push
import scan


def days_between(a, n):
    out, d = [], a
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


DAYS = days_between(dt.date(2026, 6, 1), 30)
TODAY = DAYS[-1]


def tse_rows(day):
    return [[day, "1101", "台泥", "tse", 30, 31, 29, 30 + day.day / 10, 5000]]


def otc_rows(day):
    return [[day, "6488", "環球晶", "otc", 400, 410, 395, 400 + day.toordinal() % 1000, 2000]]


def yahoo_frame(names, days):
    rows = [[d, c, n, "otc", 1, 1, 1, 100 + d.toordinal() % 1000, 3] for c, n in names.items() for d in days]
    return data._frame(rows)


def env(tmp):
    return [mock.patch.object(data, "DATA_FILE", os.path.join(tmp, "data", "p.csv.gz")),
            mock.patch.object(data.time, "sleep"),
            mock.patch.object(data.config, "HISTORY_DAYS", 30)]


def run(patches, fn):
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in reversed(patches):
            p.stop()


def test_backfill_uses_yahoo_for_otc_history():
    with tempfile.TemporaryDirectory() as tmp:
        tpex_days, yahoo_days = [], []

        def fake_tpex(day, tries=3):
            tpex_days.append(day)
            return otc_rows(day)

        def fake_yahoo(names, days):
            yahoo_days.extend(days)
            return yahoo_frame(names, days)

        p = env(tmp) + [
            mock.patch.object(data, "fetch_twse", side_effect=lambda d: tse_rows(d) if d in DAYS else []),
            mock.patch.object(data, "fetch_tpex", side_effect=fake_tpex),
            mock.patch.object(data, "fetch_yahoo_otc", side_effect=fake_yahoo),
        ]
        df, _ = run(p, lambda: data.update(TODAY))
        assert tpex_days == [TODAY], tpex_days  # 櫃買中心只用來抓今天
        assert set(yahoo_days) == set(DAYS[:-1])  # 其他日子都用 Yahoo
        otc = df[df.market == "otc"]
        assert otc.date.nunique() == len(DAYS) and otc.close.nunique() == len(DAYS)
        print("回補：上櫃歷史走 Yahoo、今天走櫃買中心 OK")


def test_purge_bad_otc_and_refill():
    with tempfile.TemporaryDirectory() as tmp:
        # 模擬舊版抓錯的資料：上櫃每天都一樣
        rows = []
        for d in DAYS:
            rows += tse_rows(d)
            rows += [[d, "6488", "環球晶", "otc", 814, 820, 800, 814, 7652.7]]
        p = env(tmp)
        run(p, lambda: data.save(data._frame(rows)))
        p = env(tmp) + [
            mock.patch.object(data, "fetch_twse", side_effect=lambda d: []),
            mock.patch.object(data, "fetch_tpex", side_effect=AssertionError("不該呼叫")),
            mock.patch.object(data, "fetch_yahoo_otc", side_effect=yahoo_frame),
        ]
        df, _ = run(p, lambda: data.update(TODAY))
        otc = df[df.market == "otc"].sort_values("date")
        assert otc.close.nunique() == len(DAYS), otc.close.unique()
        assert otc.name.iloc[0] == "環球晶"
        print("錯誤的上櫃資料已刪除、用 Yahoo 補回 OK")


def test_today_tpex_duplicate_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        rows = []
        for d in DAYS[:-1]:
            rows += tse_rows(d) + otc_rows(d)
        run(env(tmp), lambda: data.save(data._frame(rows)))
        stale = otc_rows(DAYS[-2])
        stale = [[TODAY] + r[1:] for r in stale]  # 櫃買中心回傳昨天的資料
        used = []
        p = env(tmp) + [
            mock.patch.object(data, "fetch_twse", side_effect=lambda d: tse_rows(d)),
            mock.patch.object(data, "fetch_tpex", return_value=stale),
            mock.patch.object(data, "fetch_yahoo_otc", side_effect=lambda n, ds: used.extend(ds) or yahoo_frame(n, ds)),
        ]
        df, _ = run(p, lambda: data.update(TODAY))
        assert used == [TODAY]
        last = df[(df.market == "otc") & (df.date == pd.Timestamp(TODAY))]
        assert float(last.close.iloc[0]) == 100 + TODAY.toordinal() % 1000
        print("今天的上櫃資料跟昨天一樣 → 改用 Yahoo OK")


def test_incremental_save():
    saves = []
    with tempfile.TemporaryDirectory() as tmp:
        real = data.save
        many = days_between(dt.date(2026, 1, 5), 65)
        p = env(tmp) + [
            mock.patch.object(data.config, "HISTORY_DAYS", 65),
            mock.patch.object(data, "fetch_twse", side_effect=lambda d: tse_rows(d) if d in many else []),
            mock.patch.object(data, "fetch_tpex", return_value=[]),
            mock.patch.object(data, "fetch_yahoo_otc", return_value=pd.DataFrame(columns=data.COLS)),
            mock.patch.object(data, "save", side_effect=lambda df: (saves.append(df.date.nunique()), real(df))),
        ]
        run(p, lambda: data.update(many[-1]))
    assert saves[:3] == [20, 40, 60], saves
    print("每 20 天存檔一次 OK", saves)


def test_yahoo_parser():
    idx = pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"])
    cols = pd.MultiIndex.from_product([["6488.TWO", "5347.TWO"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    vals = np.array([[1, 2, 0.5, 1.5, 1.5, 2_000_000, 10, 11, 9, 10.5, 10.5, 0]] * 3, float)
    raw = pd.DataFrame(vals, index=idx, columns=cols)
    with mock.patch("yfinance.download", return_value=raw), mock.patch.object(data.time, "sleep"):
        out = data.fetch_yahoo_otc({"6488": "環球晶", "5347": "世界"}, {dt.date(2026, 6, 2), dt.date(2026, 6, 3)})
    assert len(out) == 2 and set(out.code) == {"6488"}  # 5347 量為 0 → 丟掉
    assert out.volume.iloc[0] == 2000 and out.name.iloc[0] == "環球晶"
    print("Yahoo 資料解析 OK")


def test_push_failure_still_saves():
    with tempfile.TemporaryDirectory() as d:
        rows = []
        for day in DAYS:
            rows += tse_rows(day)
        df = data._frame(rows)
        out = os.path.join(d, "docs", "candidates.json")
        fake = mock.Mock(status_code=401, text='{"message":"Authentication failed"}')
        with mock.patch.object(scan, "OUT", out), \
             mock.patch.object(scan.data, "update", return_value=(df, True)), \
             mock.patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": "x", "LINE_USER_ID": "U1"}), \
             mock.patch.object(line_push.requests, "post", return_value=fake), \
             mock.patch("sys.argv", ["scan.py"]):
            try:
                scan.main()
                raise AssertionError("應該要以錯誤結束")
            except SystemExit as e:
                assert e.code == 1
        assert json.load(open(out))["data_date"] == TODAY.isoformat()
        print("LINE 失敗：資料照樣存檔、並標示失敗 OK")


if __name__ == "__main__":
    test_yahoo_parser()
    test_backfill_uses_yahoo_for_otc_history()
    test_purge_bad_otc_and_refill()
    test_today_tpex_duplicate_rejected()
    test_incremental_save()
    test_push_failure_still_saves()
    print("\n穩定性測試通過 ✅")
