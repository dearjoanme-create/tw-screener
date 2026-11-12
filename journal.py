"""每天的選股、盤中檢查結果，自動寫進你的 Google 試算表（跟持股同一份，只有你看得到）。

分頁：
  選股紀錄  每天晚上的預備名單，一檔一列；之後每天晚上自動補上「事後結果」
            （隔日收盤、5 日／10 日報酬、期間最高／最低、有沒有碰到停損）
  盤中紀錄  10:55／13:00 每一檔的檢查結果和原因

報酬一律用「訊號隔天的收盤價」當進場價，做空的報酬已經換算成「跌越多賺越多」。
寫入失敗不影響選股和推播。
"""
import datetime as dt
import json
import subprocess

import pandas as pd
import requests

import config as C
import holdings

SCAN_SHEET = "選股紀錄"
SCAN_HEAD = ["日期", "代號", "名稱", "市場", "多空", "型態", "位階", "趨勢", "分數", "強度", "類股",
             "收盤", "觸發價", "距觸發%", "需量倍數", "停損", "大盤", "逆大盤", "id",
             "隔日收盤", "隔日漲跌%", "隔日達觸發", "5日報酬%", "10日報酬%", "期間最高%", "期間最低%",
             "碰停損", "更新日",
             "13:00結果", "原因", "進場價", "出場日", "出場價", "報酬%", "勝負", "出場原因"]
OUTCOME_COLS = SCAN_HEAD[SCAN_HEAD.index("隔日收盤"):]
ENTERED = ("✅ 可進場", "KD 擋下", "其他條件擋下")  # 這三種模擬買賣（擋下的算「如果進場會怎樣」）
MAX_HOLD = 60  # 最多追蹤 60 個交易日
CHECK_SHEET = "盤中紀錄"
CHECK_HEAD = ["日期", "時段", "時間", "代號", "名稱", "多空", "型態", "觸發價", "現價", "狀態", "原因",
              "推估量(張)", "停損", "名單日期"]
TEXT_COLS = ["代號", "時間", "id"]  # 試算表不要自動轉成數字或時間
HORIZON = 10


def _post(body):
    url, secret = holdings._cfg()
    if not url or not secret:
        return None
    try:
        r = requests.post(url, json={"secret": secret, **body}, timeout=60)
        j = r.json()
        if not j.get("ok"):
            print(f"寫入紀錄表失敗：{j.get('error')}")
        return j
    except Exception as e:  # noqa: BLE001
        print(f"寫入紀錄表失敗：{type(e).__name__}")
        return None


def _get(action):
    url, secret = holdings._cfg()
    if not url or not secret:
        return None
    try:
        j = requests.get(url, params={"action": action, "secret": secret}, timeout=60).json()
        return j if j.get("ok") else None
    except Exception as e:  # noqa: BLE001
        print(f"讀取紀錄表失敗：{type(e).__name__}")
        return None


def _log(sheet, head, match, rows):
    return _post({"action": "log", "sheet": sheet, "head": head, "text_cols": TEXT_COLS,
                  "match": match, "rows": rows})


# ------------------------------------------------------------------
# 選股紀錄
# ------------------------------------------------------------------
def scan_rows(cand):
    mkt = cand.get("market") or {}
    label = mkt.get("label", "")
    out = []
    for side in ("long", "short"):
        against = mkt.get(f"allow_{side}") is False
        for r in cand.get(side, []):
            stop = r.get("stop") or {}
            out.append({
                "日期": cand["data_date"], "代號": r["code"], "名稱": r.get("name", ""),
                "市場": "上櫃" if r.get("market") == "otc" else "上市",
                "多空": "多" if side == "long" else "空", "型態": r.get("title", ""),
                "位階": r.get("position", ""), "趨勢": r.get("trend", ""), "分數": r.get("score", ""),
                "強度": r.get("rs", ""), "類股": r.get("sector", ""), "收盤": r.get("close", ""),
                "觸發價": r.get("trigger", ""), "距觸發%": r.get("gap_pct", ""),
                "需量倍數": r.get("need_vol", ""), "停損": stop.get("use") or stop.get("fixed", ""),
                "大盤": label, "逆大盤": "是" if against else ("短多" if side == "long" and mkt.get("long_counter") else ""),
                "13:00結果": "逆大盤" if against else "",
                "id": f"{cand['data_date']}|{r['code']}|{side}",
            })
    return out


def log_scan(cand):
    return _log(SCAN_SHEET, SCAN_HEAD, {"日期": cand["data_date"]}, scan_rows(cand))


# ------------------------------------------------------------------
# 盤中紀錄
# ------------------------------------------------------------------
def check_rows(day, run, data_date):
    out = []
    for x in run.get("results", []):
        out.append({
            "日期": day, "時段": run.get("slot", ""), "時間": run.get("time", ""), "代號": x["code"],
            "名稱": x.get("name", ""), "多空": "多" if x.get("side") == "long" else "空",
            "型態": x.get("title", ""), "觸發價": x.get("trigger", ""), "現價": x.get("price", ""),
            "狀態": x.get("status", ""),
            "原因": "、".join((x.get("miss") or []) + ([f"參考：{x['note']}"] if x.get("note") else [])),
            "推估量(張)": x.get("proj_vol", ""), "停損": x.get("stop", ""), "名單日期": data_date,
        })
    return out


def log_check(day, run, data_date):
    match = {"日期": day, "時段": run.get("slot", "")}
    if run.get("slot") == "盤中":  # 測試／補跑可能一小時跑好幾次，用時間分開
        match["時間"] = run.get("time", "")
    rows = check_rows(day, run, data_date)
    if not rows:
        return None
    return _log(CHECK_SHEET, CHECK_HEAD, match, rows)


# ------------------------------------------------------------------
# 事後結果（每天晚上用最新股價補上）
# ------------------------------------------------------------------
def _pct(x):
    return round(float(x) * 100, 2)


def outcome(g, date, side, trigger, stop):
    """g：單一股票依日期排序的 DataFrame。回傳要寫回的欄位；還沒有隔天資料就回傳 None。"""
    dates = g["date"].dt.strftime("%Y-%m-%d").tolist()
    if date not in dates:
        return None
    i0 = dates.index(date)
    if i0 + 1 >= len(g):
        return None
    c, h, l = g["close"].to_numpy(), g["high"].to_numpy(), g["low"].to_numpy()
    sign = 1 if side == "long" else -1
    e = c[i0 + 1]
    out = {"隔日收盤": float(e), "隔日漲跌%": _pct(e / c[i0] - 1)}
    try:
        trig = float(trigger)
        out["隔日達觸發"] = "是" if (e >= trig if sign > 0 else e <= trig) else "否"
    except (TypeError, ValueError):
        out["隔日達觸發"] = ""
    for n in (5, 10):
        out[f"{n}日報酬%"] = _pct(sign * (c[i0 + 1 + n] / e - 1)) if i0 + 1 + n < len(g) else ""
    w = slice(i0 + 2, min(i0 + 2 + HORIZON, len(g)))
    if w.start < w.stop:
        hi, lo = h[w].max(), l[w].min()
        best, worst = (hi / e - 1, lo / e - 1) if sign > 0 else (1 - lo / e, 1 - hi / e)
        out["期間最高%"], out["期間最低%"] = _pct(best), _pct(worst)
        try:
            s = float(stop)
            hit = (l[w] <= s).any() if sign > 0 else (h[w] >= s).any()
            out["碰停損"] = "是" if hit else "否"
        except (TypeError, ValueError):
            out["碰停損"] = ""
    else:
        out["期間最高%"] = out["期間最低%"] = out["碰停損"] = ""
    return out


def classify(status, miss):
    if status == "成形":
        return "✅ 可進場"
    if status == "價到條件不足":
        miss = miss or []
        return "KD 擋下" if miss and all(m.startswith(("KD", "K值")) for m in miss) else "其他條件擋下"
    if status == "曾到拉回":
        return "拉回"
    return "未到"


def verdicts(runs_by_date):
    """{名單日期: 13:00 確認那次的 run} → {id: (結果, 原因)}"""
    out = {}
    for dd, r in runs_by_date.items():
        for x in r.get("results", []):
            out[f"{dd}|{x['code']}|{x.get('side')}"] = (classify(x.get("status"), x.get("miss")),
                                                          "、".join(x.get("miss") or []))
    return out


def confirm_runs():
    """從 GitHub 歷史找每個名單日期最後一次 13:00 確認的結果。"""
    out = {}
    for _, c in history("docs/checks.json", deepen=0):  # 由新到舊
        dd = c.get("for_data_date")
        for r in c.get("runs", []):
            if r.get("slot") == "確認" and isinstance(r.get("results"), list) and dd not in out:
                last = [x for x in c["runs"] if x.get("slot") == "確認"][-1]
                out[dd] = last
    return out


def trade(g, date, side, stop=None):
    """照持股的短線規則模擬：訊號隔天收盤買進，之後每天收盤判斷，出現「出場」就用當天收盤賣出。"""
    from indicators import analyze

    dates = g["date"].dt.strftime("%Y-%m-%d").tolist()
    if date not in dates:
        return None
    i1 = dates.index(date) + 1
    if i1 >= len(g):
        return None
    entry = float(g["close"].iloc[i1])
    out = {"進場價": entry, "出場日": "", "出場價": "", "報酬%": "", "勝負": "持有中", "出場原因": ""}
    h = {"code": "", "side": side, "price": entry, "date": dates[i1], "mode": "短"}
    try:
        s = float(stop)
        # 建議停損要在進場價的正確一側、而且不超過 10%，否則用固定 5%
        if (side == "long" and 0 < (entry - s) / entry <= C.STOP_MAX) or (side == "short" and 0 < (s - entry) / entry <= C.STOP_MAX):
            h["stop"] = s
    except (TypeError, ValueError):
        pass
    for t in range(i1 + 1, min(len(g), i1 + 1 + MAX_HOLD)):
        ev = holdings.evaluate(analyze(g.iloc[:t + 1]), h)
        if ev["level"] == "出場":
            px = float(g["close"].iloc[t])
            r = (px / entry - 1) if side == "long" else (entry / px - 1)
            out.update({"出場日": dates[t], "出場價": px, "報酬%": _pct(r), "勝負": "賺" if r > 0 else "賠",
                        "出場原因": ev["text"]})
            return out
    if len(g) - i1 - 1 >= MAX_HOLD:
        px = float(g["close"].iloc[i1 + MAX_HOLD])
        r = (px / entry - 1) if side == "long" else (entry / px - 1)
        out.update({"出場日": dates[i1 + MAX_HOLD], "出場價": px, "報酬%": _pct(r),
                    "勝負": "賺" if r > 0 else "賠", "出場原因": f"持有滿 {MAX_HOLD} 天"})
    return out


def update_outcomes(df, pending=None, judged=None):
    pending = pending if pending is not None else (_get("signals") or {}).get("items", [])
    if not pending:
        return 0
    codes = {str(p["code"]).lstrip("'") for p in pending}
    sub = df[df["code"].astype(str).isin(codes)].sort_values(["code", "date"])
    groups = {k: g.reset_index(drop=True) for k, g in sub.groupby(sub["code"].astype(str))}
    today = dt.date.today().isoformat()
    if judged is None:
        try:
            judged = verdicts(confirm_runs())
        except Exception as e:  # noqa: BLE001
            print(f"讀 13:00 結果失敗：{type(e).__name__} {e}")
            judged = {}
    items = []
    for p in pending:
        g = groups.get(str(p["code"]).lstrip("'"))
        if g is None:
            continue
        side = "long" if p.get("side") in ("多", "long") else "short"
        date = str(p["date"])[:10]
        o = outcome(g, date, side, p.get("trigger"), p.get("stop")) or {}
        res = p.get("result") or ""
        if res != "逆大盤" and p["id"] in judged:
            res, why = judged[p["id"]]
            o.update({"13:00結果": res, "原因": why})
        if res in ENTERED:
            try:
                o.update(trade(g, date, side, p.get("stop")) or {})
            except Exception as e:  # noqa: BLE001
                print(f"{p['id']} 模擬買賣失敗：{type(e).__name__} {e}")
        if o:
            items.append({"id": p["id"], "values": {**o, "更新日": today}})
    if items:
        _post({"action": "outcomes", "sheet": SCAN_SHEET, "head": SCAN_HEAD, "items": items})
    return len(items)


# ------------------------------------------------------------------
# 第一次使用：從 GitHub 的歷史版本把以前的資料補進去
# ------------------------------------------------------------------
def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def history(path, deepen=300):
    """回傳 [(commit 標題, 檔案內容 dict)]，由新到舊。"""
    try:
        if deepen:
            _git("fetch", "--quiet", f"--deepen={deepen}", "origin")
    except Exception:  # noqa: BLE001
        pass  # 不是 shallow clone 或沒網路，就用現有的歷史
    out = []
    for line in _git("log", "--format=%h %s", "--", path).splitlines():
        h, _, subject = line.partition(" ")
        try:
            out.append((subject, json.loads(_git("show", f"{h}:{path}"))))
        except Exception:  # noqa: BLE001
            continue
    return out


def backfill(skip_date=None):
    """把以前每天的選股和盤中檢查補進試算表（只在「選股紀錄」是空的時候跑一次）。"""
    n_scan = n_check = 0
    seen = set()
    for _, c in history("docs/candidates.json"):
        d = c.get("data_date")
        if not c.get("version") or not d or d in seen or d == skip_date:
            continue
        seen.add(d)
        rows = scan_rows(c)
        if rows:
            _log(SCAN_SHEET, SCAN_HEAD, {"日期": d}, rows)
            n_scan += len(rows)
    runs = {}
    for subject, c in history("docs/checks.json"):
        if not subject.startswith("check "):
            continue
        day = subject.split()[1][:10]  # 「check 2026-11-10_0255」→ 執行日期（UTC 日期跟台灣上午相同）
        for r in c.get("runs", []):
            if not isinstance(r.get("results"), list) or not r.get("slot"):
                continue  # 舊版格式，略過
            runs.setdefault((day, r.get("slot"), r.get("time")), (r, c.get("for_data_date", "")))
    for (day, _, _), (r, dd) in sorted(runs.items()):
        if log_check(day, r, dd):
            n_check += len(r.get("results", []))
    print(f"補進以前的資料：選股 {n_scan} 筆、盤中 {n_check} 筆")


def nightly(cand, df):
    """scan.py 每天晚上呼叫：（第一次先補舊資料）→ 寫今天的名單 → 補事後結果。"""
    info = _get("signals")
    if info is None:
        return
    try:
        _git("fetch", "--quiet", "--deepen=300", "origin")  # 要讀以前的 13:00 結果
    except Exception:  # noqa: BLE001
        pass
    if info.get("total", 0) == 0:
        try:
            backfill(skip_date=cand["data_date"])
        except Exception as e:  # noqa: BLE001
            print(f"補舊資料失敗（不影響今天）：{type(e).__name__} {e}")
    log_scan(cand)
    n = update_outcomes(df)
    print(f"紀錄表：今天 {len(scan_rows(cand))} 檔、更新事後結果 {n} 筆")
