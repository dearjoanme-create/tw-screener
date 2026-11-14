"""一次性：補抓上市融資歷史（每天的融資金額、融資張數、估算融資維持率），存到 data/margin_hist.csv。
可以重複執行，已經抓過的日子會跳過。給回測的融資濾網用。

融資維持率（估算）＝ Σ(每檔融資餘額張數 × 1000 × 當天收盤價) ÷ 融資金額
"""
import json
import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data import HEADERS, _margin_table, _find_col, _num  # noqa: E402

OUT = "data/margin_hist.csv"
URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"


def summary_amount(tables):
    """信用交易統計表裡「融資金額(仟元)」的今日餘額 → 億元。"""
    for t in tables:
        fields = [str(f).replace(" ", "") for f in (t.get("fields") or [])]
        if "今日餘額" not in fields:
            continue
        col = fields.index("今日餘額")
        for r in t.get("data") or []:
            if "融資金額" in str(r[0]):
                v = _num(r[col])
                return None if v is None else v / 1e5
    return None


def fetch(day):
    j = requests.get(URL, params={"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"},
                     headers=HEADERS, timeout=30).json()
    if j.get("stat") not in (None, "OK"):
        return None, None, j.get("stat")
    tables = j.get("tables") or [{"fields": j.get("fields"), "data": j.get("data")}]
    amt = summary_amount(tables)
    per = {}
    for t in tables:
        if _find_col(t.get("fields") or [], "代號") is not None and len(t.get("data") or []) > 50:
            per = _margin_table(t["fields"], t["data"])
            break
    if amt is None:
        print(day, "找不到融資金額，表格：", json.dumps([t.get("fields") for t in tables], ensure_ascii=False)[:400])
    return amt, per, None


def main():
    idx = pd.read_csv("data/taiex.csv", parse_dates=["date"])
    prices = pd.read_csv("data/prices.csv.gz", dtype={"code": str}, parse_dates=["date"])
    prices = prices[prices["market"] == "tse"] if "market" in prices else prices
    close = prices.pivot_table(index="date", columns="code", values="close")
    old = pd.read_csv(OUT, parse_dates=["date"]) if os.path.exists(OUT) else pd.DataFrame(columns=["date"])
    have = set(old["date"].dt.date) if len(old) else set()
    rows, fails = [], 0
    for d in idx["date"]:
        if d.date() in have:
            continue
        try:
            amt, per, err = fetch(d)
        except Exception as e:  # noqa: BLE001
            print(d.date(), "抓取失敗", type(e).__name__)
            fails += 1
            time.sleep(10)
            continue
        if err or amt is None:
            print(d.date(), "沒有資料", err)
            time.sleep(4)
            continue
        lots = sum(v[1] or 0 for v in per.values())
        mv = 0.0
        if d in close.index:
            cl = close.loc[d]
            mv = sum((v[1] or 0) * 1000 * cl.get(code, float("nan")) for code, v in per.items()
                     if v[1] and pd.notna(cl.get(code)))
        maint = round(mv / (amt * 1e8) * 100, 1) if amt and mv else None
        rows.append({"date": d.date(), "margin_amt": round(amt, 2), "margin_lots": int(lots), "maint": maint})
        print(d.date(), f"融資 {amt:,.1f} 億，維持率 {maint}", flush=True)
        time.sleep(4)  # 證交所限制：太快會被暫時封鎖
        if len(rows) % 20 == 0:
            save(old, rows)
    save(old, rows)
    print("完成，新增", len(rows), "天，失敗", fails, "次")


def save(old, rows):
    df = pd.concat([old, pd.DataFrame(rows)], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df.drop_duplicates("date", keep="last").sort_values("date").to_csv(OUT, index=False)


if __name__ == "__main__":
    main()
