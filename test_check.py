"""用假的即時報價測試盤中檢查。執行：python -m tests.test_check"""
import datetime as dt
import json
import os
import tempfile
from unittest import mock

import check

cand = {
    "data_date": "2026-10-28",
    "patterns": {
        "pullback": [{"code": "1101", "name": "甲", "market": "tse", "close": 100, "trigger": 102, "vol5": 1000, "vol_ratio": 0}],
        "trendline": [{"code": "2201", "name": "乙", "market": "otc", "close": 50, "trigger": 51, "vol5": 2000, "vol_ratio": 1.2}],
        "range": [{"code": "3301", "name": "丙", "market": "tse", "close": 30, "trigger": 31, "vol5": 3000, "vol_ratio": 1.5}],
    },
}
quotes = {
    "1101": {"price": 103, "high": 103.5, "vol": 300, "date": "20261029", "time": "11:00:00"},
    "2201": {"price": 50.5, "high": 51.2, "vol": 500, "date": "20261029", "time": "11:00:00"},
    "3301": {"price": 31.5, "high": 31.6, "vol": 3000, "date": "20261029", "time": "11:00:00"},
}


def test():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "candidates.json"), "w") as f:
            json.dump(cand, f)
        fake_now = dt.datetime(2026, 10, 29, 11, 0, tzinfo=check.TW)
        sent = []
        with mock.patch.object(check, "DOCS", d), \
             mock.patch.object(check, "fetch_quotes", return_value=quotes), \
             mock.patch.object(check.line_push, "push", side_effect=lambda t: sent.append(t) or True), \
             mock.patch.object(check.dt, "datetime", wraps=dt.datetime) as m:
            m.now.return_value = fake_now
            check.main()
        print(sent[0])
        res = json.load(open(os.path.join(d, "checks.json")))
        r = {x["code"]: x for k in res["runs"][0]["results"].values() for x in k}
        assert r["1101"]["status"] == "突破" and r["1101"]["vol_ok"]
        assert r["2201"]["status"] == "曾突破拉回"
        # 3301：120 分鐘 3000 張 → 預估 6750 張 > 1.5×3000=4500
        assert r["3301"]["status"] == "突破" and r["3301"]["vol_ok"]
    print("\n盤中檢查測試通過 ✅")


if __name__ == "__main__":
    test()
