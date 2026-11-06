"""每個交易日晚上執行：更新資料 → 大盤狀態 → 找多空進場位置 → 存成網頁資料 → LINE 推播。
本機測試：python scan.py            （用今天日期）
          python scan.py --force    （同一天重跑也照樣推播）
          python scan.py 2026-10-28 （指定日期）
"""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd

import config
import data
import line_push
import market
from indicators import analyze
from patterns import TITLES, confirmed_today, evaluate

TW = ZoneInfo("Asia/Taipei")
OUT = os.path.join(os.path.dirname(__file__), "docs", "candidates.json")
EXCLUDE_FILE = os.path.join(os.path.dirname(__file__), "exclude.txt")
MAX_EXCLUDED = 80  # 網頁「被排除」最多列幾檔
NO_SHORT = "不能融券（不是信用交易標的，或暫停融券）"


def load_exclude():
    if not os.path.exists(EXCLUDE_FILE):
        return set()
    with open(EXCLUDE_FILE, encoding="utf-8") as f:
        return {ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()}


def rs_rank(df, last_date):
    """相對強度：近 RS_DAYS 天漲幅在全市場的百分位（0–100）。"""
    rets = {}
    for code, g in df.groupby("code"):
        if g["date"].iloc[-1] != last_date or len(g) <= config.RS_DAYS:
            continue
        c = g["close"].to_numpy(float)
        rets[code] = c[-1] / c[-1 - config.RS_DAYS] - 1
    s = pd.Series(rets, dtype=float)
    if s.empty:
        return {}
    return (s.rank(pct=True) * 100).round().astype(int).to_dict()


def screen(df, last_date, short_ok=None):
    """short_ok：data.shortable() 的結果；某市場是 None 代表抓不到、不過濾。"""
    exclude = load_exclude()
    df = df.sort_values(["code", "date"])
    rs = rs_rank(df, last_date)
    out = {"long": [], "short": [], "confirmed": [], "excluded": []}
    for code, g in df.groupby("code"):
        if code in exclude or g["date"].iloc[-1] != last_date or len(g) < config.MIN_BARS:
            continue  # 今天沒交易（停牌等）或資料太短就跳過
        last = g.iloc[-1]
        vol5 = g["volume"].tail(5).mean()
        if last["close"] < config.MIN_PRICE or vol5 < config.MIN_AVG_VOL_LOTS:
            continue
        r = rs.get(code)
        try:
            a = analyze(g)
            res = evaluate(a, a.t, r)
            conf = confirmed_today(a, r)
        except Exception as e:  # noqa: BLE001
            print(f"{code} 判斷錯誤：{type(e).__name__} {e}")
            continue
        base = {"code": code, "name": last["name"], "market": last["market"]}
        table = (short_ok or {}).get(last["market"])
        can_short = None if table is None else bool(table.get(code, False))
        if res["short"] and can_short is False:
            res["excluded"].append({**res["short"], "why": [NO_SHORT]})
            res["short"] = None
        conf = [x for x in conf if not (x["side"] == "short" and can_short is False)]
        for slot in ("long", "short"):
            if res[slot]:
                out[slot].append({**base, **res[slot]})
        out["excluded"] += [{**base, **x} for x in res["excluded"]]
        out["confirmed"] += [{**base, **x} for x in conf]
    # 今天收盤已經走出進場K的，不再列入明天的預備名單
    done = {(x["code"], x["side"]) for x in out["confirmed"]}
    for slot in ("long", "short"):
        out[slot] = [x for x in out[slot] if (x["code"], slot) not in done]
    for k in out:
        out[k].sort(key=lambda x: (-x["score"], x["gap_pct"]))
    out["excluded"] = out["excluded"][:MAX_EXCLUDED]
    return out


def _n(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def message(res, mkt, last_date, short_ok=None):
    lines = [f"📊 {last_date:%m/%d} 收盤選股"]
    sides = ("long", "short")
    if mkt:
        lines.append(f"大盤 {_n(mkt['close'])}（{mkt['chg_pct']:+.2f}%）｜{mkt['label']}")
        lines.append(f"月線 {_n(mkt['ma20'])} {mkt['ma20_dir']}｜建議資金 {mkt['capital']}")
        sides = [s for s in sides if mkt[f"allow_{s}"]]
        lines.append("今天只推" + ("多方" if sides == ["long"] else "空方" if sides == ["short"] else "多空"))
    for side in sides:
        items = res[side]
        name = "多方" if side == "long" else "空方"
        lines.append(f"\n【{name}預備】{len(items)} 檔｜明天 10:55 預警、13:00 確認")
        if side == "short" and short_ok is not None and None in short_ok.values():
            lines.append("⚠️ 今天融券資料抓不到，放空前請先確認能否融券")
        if not items:
            lines.append("今天沒有符合的股票")
        for i, r in enumerate(items[:config.LINE_TOP_N], 1):
            arrow = "≥" if side == "long" else "≤"
            vol = f"、量≥{r['need_vol']}倍" if r["need_vol"] else ""
            lines.append(f"{i}. {r['code']} {r['name']}｜{r['title']}｜{r['position']}｜{r['score']}分")
            lines.append(f"  收{_n(r['close'])}→收盤要{arrow}{_n(r['trigger'])}（{r['gap_pct']:.1f}%）{vol}")
            lines.append(f"  停損{_n(r['stop']['fixed'])}" + (f"／趨勢{_n(r['stop']['trend'])}" if r["stop"]["trend"] else ""))
        if len(items) > config.LINE_TOP_N:
            lines.append(f"…其餘 {len(items) - config.LINE_TOP_N} 檔看網頁")
    conf = [r for r in res["confirmed"] if not mkt or mkt[f"allow_{r['side']}"]]
    if conf:
        lines.append(f"\n【今日收盤已確認】{len(conf)} 檔（昨晚名單，今天走出進場K）")
        for r in conf[:config.LINE_CONFIRMED_N]:
            side = "多" if r["side"] == "long" else "空"
            lines.append(f"✅{r['code']} {r['name']} {side}・{r['title']} 收{_n(r['close'])}（{r['chg_pct']:+.1f}%）"
                         f" 停損{_n(r['stop']['fixed'])}")
    url = line_push.dashboard_url()
    if url:
        lines.append(f"\n完整清單：{url}")
    lines.append("僅供參考，依紀律操作、自行判斷")
    return "\n".join(lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv  # 手動執行時強制重新產生並推播
    if args:
        today = dt.date.fromisoformat(args[0])
    else:
        today = dt.datetime.now(TW).date()

    df, _ = data.update(today)
    if df.empty:
        print("沒有抓到任何資料，請看上面的錯誤訊息。")
        return
    last_date = df["date"].max()
    # 沒有新的收盤資料（假日、颱風假）就不重複推播
    try:
        if force:
            raise FileNotFoundError
        with open(OUT, encoding="utf-8") as f:
            if json.load(f)["data_date"] == last_date.strftime("%Y-%m-%d"):
                print(f"{today} 沒有新的收盤資料（休市），今天不推播。")
                return
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        pass

    try:
        idx = data.update_index(today)
        idx = idx[idx["date"] <= last_date]
        mkt = market.status(idx)
    except Exception as e:  # noqa: BLE001
        print(f"大盤資料有問題，這次不做大盤過濾：{e}")
        mkt = None

    try:
        short_ok = data.shortable(last_date.date())
    except Exception as e:  # noqa: BLE001
        print(f"融資融券資料有問題，這次放空不過濾：{e}")
        short_ok = {"tse": None, "otc": None}

    res = screen(df, last_date, short_ok)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    payload = {
        "version": 2,
        "data_date": last_date.strftime("%Y-%m-%d"),
        "generated_at": dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M"),
        "market": mkt,
        "titles": TITLES,
        "short_checked": {k: v is not None for k, v in short_ok.items()},
        **res,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    # 新的一天，清掉昨天的盤中檢查結果
    checks = os.path.join(os.path.dirname(OUT), "checks.json")
    with open(checks, "w", encoding="utf-8") as f:
        json.dump({"for_data_date": payload["data_date"], "runs": []}, f, ensure_ascii=False)

    print(f"多方 {len(res['long'])} 檔、空方 {len(res['short'])} 檔、今日確認 {len(res['confirmed'])} 檔")
    if not line_push.push(message(res, mkt, last_date, short_ok)):
        sys.exit(1)  # 讓 GitHub 顯示紅叉提醒你，但資料已經存好了


if __name__ == "__main__":
    main()
