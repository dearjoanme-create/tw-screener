"""用假的即時報價測試盤中檢查。執行：python -m tests.test_check"""
import datetime as dt
import json
import os
import tempfile
from unittest import mock

import check

REF = {"k": 50.0, "low_n1": 95.0, "high_n1": 105.0, "ma20": 98.0}
cand = {
    "version": 2,
    "data_date": "2026-11-04",
    "market": {"allow_long": True, "allow_short": True},
    "long": [
        {"code": "1101", "name": "甲", "market": "tse", "side": "long", "title": "回後買上漲", "close": 100,
         "trigger": 102, "vol5": 1000, "need_vol": 1.0, "ref": REF, "stop": {"fixed": 96.9}},
        {"code": "2201", "name": "乙", "market": "otc", "side": "long", "title": "突破盤整上頸線", "close": 50,
         "trigger": 51, "vol5": 2000, "need_vol": 1.2, "ref": {**REF, "low_n1": 47, "high_n1": 52}, "stop": {"fixed": 48.4}},
        {"code": "3301", "name": "丙", "market": "tse", "side": "long", "title": "一字底突破", "close": 30,
         "trigger": 31, "vol5": 3000, "need_vol": 1.2, "ref": {**REF, "low_n1": 29, "high_n1": 31}, "stop": {"fixed": 29.45}},
    ],
    "short": [
        {"code": "4401", "name": "丁", "market": "tse", "side": "short", "title": "彈後空下跌", "close": 80,
         "trigger": 78, "vol5": 1000, "need_vol": 0, "ref": {**REF, "low_n1": 76, "high_n1": 84}, "stop": {"fixed": 81.9}},
    ],
}
quotes = {
    # 實體紅K、漲 3.5%、量夠 → 成形
    "1101": {"price": 103.5, "open": 100.2, "high": 104, "low": 100, "prev": 100, "vol": 1200, "date": "20261105", "time": "13:10:00"},
    # 價到了，但上影線太長
    "2201": {"price": 51.1, "open": 50.1, "high": 53.5, "low": 50, "prev": 50, "vol": 2500, "date": "20261105", "time": "13:10:00"},
    # 曾到觸發價又拉回
    "3301": {"price": 30.4, "open": 30.1, "high": 31.2, "low": 30, "prev": 30, "vol": 3000, "date": "20261105", "time": "13:10:00"},
    # 做空：實體黑K、跌 3%
    "4401": {"price": 77.6, "open": 80.2, "high": 80.4, "low": 77.4, "prev": 80, "vol": 900, "date": "20261105", "time": "13:10:00"},
    # 加權指數（只用來判斷有沒有開盤）
    "t00": {"price": 40000, "open": 40000, "high": 40100, "low": 39900, "prev": 40000, "vol": 0, "date": "20261105", "time": "13:10:00"},
}


shares = []


def run_at(d, hour, minute, force=False, held=None):
    sent = []
    fake_now = dt.datetime(2026, 11, 5, hour, minute, tzinfo=check.TW)
    with mock.patch.object(check, "DOCS", d), \
         mock.patch.object(check, "fetch_quotes", return_value=quotes), \
         mock.patch.object(check.holdings, "fetch", return_value=held) as hf, \
         mock.patch.object(check.line_push, "push", side_effect=lambda t, share=False: sent.append(t) or shares.append(share) or True), \
         mock.patch.object(check.dt, "datetime", wraps=dt.datetime) as m, \
         mock.patch("sys.argv", ["check.py"] + (["--force"] if force else [])):
        m.now.return_value = fake_now
        check.main()
        run_at.fetched = hf.called
    return sent


def test():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "candidates.json"), "w") as f:
            json.dump(cand, f)
        sent = run_at(d, 13, 10)
        print(sent[0])
        res = json.load(open(os.path.join(d, "checks.json")))
        run = res["runs"][0]
        assert run["slot"] == "確認"
        r = {x["code"]: x for x in run["results"]}
        assert r["1101"]["status"] == "成形", r["1101"]
        assert r["2201"]["status"] == "價到條件不足" and any("上影線" in m for m in r["2201"]["miss"])
        assert r["3301"]["status"] == "曾到拉回"
        assert r["4401"]["status"] == "成形", r["4401"]

        # 同一時段第二次觸發（例如外部定時器＋GitHub 排程）不重複推播
        assert run_at(d, 13, 14) == []
        # 加 --force 可以強制重跑
        assert len(run_at(d, 13, 15, force=True)) == 1
        # 早上的預警是另一個時段
        assert len(run_at(d, 11, 0)) == 1
        assert [x["slot"] for x in json.load(open(os.path.join(d, "checks.json")))["runs"]] == ["確認", "確認", "預警"]

        # 12 點多測試或補跑：算「盤中」，會推播，而且不會擋掉 13:00 的確認
        os.remove(os.path.join(d, "checks.json"))
        assert len(run_at(d, 12, 32)) == 1
        assert len(run_at(d, 12, 40)) == 1
        assert len(run_at(d, 13, 0)) == 1
        assert run_at(d, 13, 5) == []   # GitHub 備援 13:05 → 確認已推過，跳過
        assert [x["slot"] for x in json.load(open(os.path.join(d, "checks.json")))["runs"]] == ["盤中", "盤中", "確認"]

        # 大盤在月線上：空方不檢查
        c2 = {**cand, "market": {"allow_long": True, "allow_short": False}}
        with open(os.path.join(d, "candidates.json"), "w") as f:
            json.dump(c2, f)
        os.remove(os.path.join(d, "checks.json"))
        run_at(d, 13, 10)
        codes = {x["code"] for x in json.load(open(os.path.join(d, "checks.json")))["runs"][0]["results"]}
        assert "4401" not in codes
        # 持股：放在 10:55 預警（只推給你）；13:00 確認推給朋友，不放持股
        os.remove(os.path.join(d, "checks.json"))
        held = [{"code": "1101", "name": "甲", "market": "tse", "side": "long", "price": 110, "mode": "短",
                 "advice": {"stop": 105, "sum4": 440}},
                {"code": "4401", "name": "丁", "market": "tse", "side": "long", "price": 70, "mode": "短",
                 "advice": {"stop": 66.5, "sum4": 300}}]
        shares.clear()
        msg = run_at(d, 10, 55, held=held)[0]
        print(msg)
        assert run_at.fetched and "持股提醒" in msg and "跌破停損" in msg and shares[-1] is False
        assert msg.index("持股提醒") < msg.index("目前符合")
        assert "1101" not in json.dumps(json.load(open(os.path.join(d, "checks.json"))).get("runs")[-1].get("holdings", ""))
        # 13:00：確認推給你和朋友（不含持股）；持股有狀況另推一則只給你
        shares.clear()
        sent = run_at(d, 13, 0, held=held)
        print(sent[-1])
        assert len(sent) == 2 and "持股" not in sent[0] and shares == [True, False]
        assert "只有你收到" in sent[1] and "跌破停損" in sent[1]
        # 持股都沒事：13:00 不多推
        os.remove(os.path.join(d, "checks.json"))
        calm = [{**held[1], "advice": {"stop": 1, "sum4": 1}}]
        shares.clear()
        sent = run_at(d, 13, 0, held=calm)
        assert len(sent) == 1 and shares == [True]
        # 沒有預備股、只有持股出狀況：13:00 只推給你
        with open(os.path.join(d, "candidates.json"), "w") as f:
            json.dump({**cand, "long": [], "short": []}, f)
        os.remove(os.path.join(d, "checks.json"))
        shares.clear()
        sent = run_at(d, 13, 0, held=held)
        print(sent[0])
        assert len(sent) == 2 and shares == [True, False] and "今天沒有需要檢查" in sent[0] and "只有你收到" in sent[1]
        # 大盤在月線下、空方 0 檔、沒有持股：也要推一則說明，不能安靜跳過
        with open(os.path.join(d, "candidates.json"), "w") as f:
            json.dump({**cand, "market": {"allow_long": False, "allow_short": True}, "short": []}, f)
        for hm in ((10, 55), (13, 0)):
            os.remove(os.path.join(d, "checks.json"))
            shares.clear()
            sent = run_at(d, *hm, held=[])
            print(sent[0])
            assert len(sent) == 1 and "空方預備 0 檔" in sent[0] and "多方 3 檔逆大盤" in sent[0]
            assert shares == [hm[0] == 13]
        # 假日（報價日期不是今天）：不推
        os.remove(os.path.join(d, "checks.json"))
        with mock.patch.dict(quotes, {k: {**v, "date": "20261104"} for k, v in quotes.items()}):
            assert run_at(d, 13, 0, held=[]) == []
    print("\n盤中檢查測試通過 ✅")


if __name__ == "__main__":
    test()
