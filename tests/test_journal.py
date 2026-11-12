"""紀錄表：事後結果計算。執行：python -m tests.test_journal"""
import pandas as pd

import journal


def test():
    days = pd.bdate_range("2026-11-02", periods=16)
    close = [100, 100, 102, 104, 103, 105, 106, 104, 108, 110, 109, 111, 112, 113, 114, 115]
    g = pd.DataFrame({"date": days, "close": close, "high": [c + 1 for c in close], "low": [c - 1 for c in close]})
    o = journal.outcome(g, "2026-11-03", "long", 101, 97)
    assert o["隔日收盤"] == 102 and o["隔日達觸發"] == "是" and o["隔日漲跌%"] == 2.0
    assert o["5日報酬%"] == round((104 / 102 - 1) * 100, 2) and o["碰停損"] == "否"
    s = journal.outcome(g, "2026-11-03", "short", 99, 106)
    assert s["隔日達觸發"] == "否" and s["5日報酬%"] < 0 and s["碰停損"] == "是"
    assert journal.outcome(g, days[-1].strftime("%Y-%m-%d"), "long", 1, 1) is None  # 還沒有隔天
    late = journal.outcome(g, days[-3].strftime("%Y-%m-%d"), "long", 1, 1)
    assert late["5日報酬%"] == "" and late["10日報酬%"] == ""
    rows = journal.scan_rows({"data_date": "2026-11-10", "market": {"label": "空頭", "allow_long": False, "allow_short": True},
                              "long": [{"code": "2330", "title": "x", "stop": {"fixed": 1}}], "short": []})
    assert rows[0]["逆大盤"] == "是" and rows[0]["id"] == "2026-11-10|2330|long" and set(rows[0]) <= set(journal.SCAN_HEAD)
    assert journal.classify("價到條件不足", ["KD 還沒轉弱"]) == "KD 擋下"
    assert journal.classify("價到條件不足", ["KD 還沒轉弱", "量不夠"]) == "其他條件擋下"
    assert journal.classify("成形", []) == "✅ 可進場" and journal.classify("未到", []) == "未到"
    print("紀錄表測試通過 ✅")


if __name__ == "__main__":
    test()
