"""每個交易日晚上執行：更新資料 → 找三種型態 → 存成網頁資料 → LINE 推播。
本機測試：python scan.py            （用今天日期）
          python scan.py --force    （同一天重跑也照樣推播）
          python scan.py 2026-10-28 （指定日期）
"""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import config
import data
import line_push
from patterns import PATTERNS

TW = ZoneInfo("Asia/Taipei")
OUT = os.path.join(os.path.dirname(__file__), "docs", "candidates.json")
EXCLUDE_FILE = os.path.join(os.path.dirname(__file__), "exclude.txt")


def load_exclude():
    if not os.path.exists(EXCLUDE_FILE):
        return set()
    with open(EXCLUDE_FILE, encoding="utf-8") as f:
        return {ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()}


def screen(df, last_date):
    exclude = load_exclude()
    results = {k: [] for k in PATTERNS}
    for code, g in df.groupby("code"):
        g = g.sort_values("date")
        if code in exclude or g["date"].iloc[-1] != last_date:
            continue  # 今天沒交易（停牌等）就跳過
        last = g.iloc[-1]
        vol5 = g["volume"].tail(5).mean()
        if last["close"] < config.MIN_PRICE or vol5 < config.MIN_AVG_VOL_LOTS:
            continue
        for key, (_, fn) in PATTERNS.items():
            try:
                hit = fn(g)
            except Exception as e:  # noqa: BLE001
                print(f"{code} {key} 判斷錯誤：{e}")
                continue
            if hit:
                results[key].append({
                    "code": code,
                    "name": last["name"],
                    "market": last["market"],
                    "close": round(float(last["close"]), 2),
                    "trigger": hit["trigger"],
                    "gap_pct": round((hit["trigger"] / last["close"] - 1) * 100, 2),
                    "vol5": int(round(vol5)),
                    "vol_ratio": hit["vol_ratio"],
                    "note": hit["note"],
                    "score": hit["score"],
                })
    for key in results:
        results[key].sort(key=lambda x: x["score"])
        for r in results[key]:
            r.pop("score")
    return results


def message(results, last_date):
    lines = [f"📊 選股清單（{last_date:%m/%d} 收盤）", "明天盤中 11:00、13:00 會自動檢查是否突破"]
    for key, (title, _) in PATTERNS.items():
        items = results[key]
        lines.append(f"\n【{title}】共 {len(items)} 檔")
        for r in items[:config.LINE_TOP_N]:
            lines.append(f"{r['code']} {r['name']} 收{r['close']:g} → 觸發 {r['trigger']:g}（+{r['gap_pct']:.1f}%）")
        if len(items) > config.LINE_TOP_N:
            lines.append(f"…其餘 {len(items) - config.LINE_TOP_N} 檔看網頁")
    url = line_push.dashboard_url()
    if url:
        lines.append(f"\n完整清單：{url}")
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
    results = screen(df, last_date)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    payload = {
        "data_date": last_date.strftime("%Y-%m-%d"),
        "generated_at": dt.datetime.now(TW).strftime("%Y-%m-%d %H:%M"),
        "titles": {k: v[0] for k, v in PATTERNS.items()},
        "patterns": results,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    # 新的一天，清掉昨天的盤中檢查結果
    checks = os.path.join(os.path.dirname(OUT), "checks.json")
    with open(checks, "w", encoding="utf-8") as f:
        json.dump({"for_data_date": payload["data_date"], "runs": []}, f, ensure_ascii=False)

    line_push.push(message(results, last_date))


if __name__ == "__main__":
    main()
