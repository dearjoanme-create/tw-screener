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
import holdings
import journal
import line_push
import market
from indicators import analyze
from patterns import TITLES, confirmed_today, evaluate

TW = ZoneInfo("Asia/Taipei")
OUT = os.path.join(os.path.dirname(__file__), "docs", "candidates.json")
EXCLUDE_FILE = os.path.join(os.path.dirname(__file__), "exclude.txt")
MAX_EXCLUDED = 80  # 網頁「被排除」最多列幾檔
MAX_WATCH = 60     # 網頁「鎖股名單」最多列幾檔
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


def sector_rank(df, last_date, industry):
    """類股強弱：每個產業近 20 天平均漲幅排名 → {產業: (名次, 產業數)}（至少 3 檔才排）。"""
    if not industry:
        return {}
    rets = {}
    for code, g in df.groupby("code"):
        if g["date"].iloc[-1] != last_date or len(g) <= 20 or code not in industry:
            continue
        c = g["close"].to_numpy(float)
        rets.setdefault(industry[code], []).append(c[-1] / c[-21] - 1)
    avg = {k: sum(v) / len(v) for k, v in rets.items() if len(v) >= 3}
    order = sorted(avg, key=lambda k: -avg[k])
    return {k: (i + 1, len(order)) for i, k in enumerate(order)}


def chips_text(ch):
    if not ch:
        return ""
    parts = [f"法人{ch['days']}日 {ch['total']:+,.0f}張（外資 {ch['foreign']:+,.0f}、投信 {ch['trust']:+,.0f}）"]
    if ch["streak"] >= 2:
        parts.append(f"連買{ch['streak']}天")
    elif ch["streak"] <= -2:
        parts.append(f"連賣{-ch['streak']}天")
    if ch["margin_chg"] is not None:
        parts.append(f"融資 {ch['margin_chg']:+.0%}")
    return "、".join(parts)


def add_context(item, side, ch, sec, sec_rank):
    """把籌碼、類股加進檢核表和分數。"""
    extra = 0
    if ch:
        inst = ch["foreign"] + ch["trust"]
        good = inst > 0 if side == "long" else inst < 0
        hot = ch["margin_chg"] is not None and (ch["margin_chg"] >= 0.10 if side == "long" else ch["margin_chg"] <= -0.10)
        extra += 3 if good else -2
        extra -= 3 if hot else 0
        item["checks"].append({"step": "籌碼", "ok": bool(good), "text": chips_text(ch)
                               + ("（融資大增，散戶追價）" if hot and side == "long" else "")})
    if sec:
        rank, n = sec_rank.get(sec, (None, None))
        if rank:
            strong = rank <= 3 if side == "long" else rank > n - 3
            extra += 5 if strong else 0
            word = f"第 {rank} 強" if rank <= n / 2 else f"倒數第 {n - rank + 1}"
            item["checks"].append({"step": "類股", "ok": bool(strong or (rank <= n / 2) == (side == "long")),
                                   "text": f"{sec}（{word}／{n} 類）"})
            item["sector"] = f"{sec} {word}"
    item["score"] = int(max(0, min(100, item["score"] + extra)))


def record(a, base, res, rs, ch, sec, sec_rank, basic_ok):
    """LINE 查個股用的摘要（每檔都存）。"""
    from indicators import TREND_NAME, position, volume_tags

    t = a.t
    tr = a.trend[t]
    pos = position(a.c, a.h, a.l, a.pivots20, t, -1 if tr == -1 else 1)
    vt, vr = volume_tags(a.o, a.h, a.l, a.c, a.v, a.vbase, a.trend, t, a.high_zone, a.low_zone)
    if a.bull_aligned(t):
        ma = "四線多排"
    elif a.bear_aligned(t):
        ma = "四線空排"
    else:
        ma = "月線上揚" if a.ma_up(20, t) else "月線下彎" if a.ma_down(20, t) else "月線走平"
        ma += "，站上月線" if a.c[t] > a.ma[20][t] else "，在月線下"
    setups = []
    for x in [res["long"], res["short"]] + res["excluded"]:
        if x and len(setups) < 3:
            setups.append({"side": x["side"], "title": x["title"], "trigger": x["trigger"],
                           "stop": x["stop"]["fixed"], "trend_stop": x["stop"]["trend"],
                           "need_vol": x["need_vol"], "why": x.get("why", [])})
    rank = sec_rank.get(sec, (None, None))[0] if sec else None
    return {
        **base,
        "date": a.df["date"].iloc[t].strftime("%Y-%m-%d"),
        "close": round(float(a.c[t]), 2),
        "chg": round(float(a.c[t] / a.c[t - 1] - 1) * 100, 2),
        "trend": TREND_NAME[tr],
        "ma": ma,
        "ma5": round(float(a.ma[5][t]), 2),
        "ma20": round(float(a.ma[20][t]), 2),
        "ma60": round(float(a.ma[60][t]), 2),
        "pos": f"{pos['name']}（第{pos['wave']}波，{pos['gain']:+.0%}）",
        "vol": "、".join(vt + [f"{x}（{-w}天前）" for x, w in vr]) or "量平",
        "kd": f"K {a.k[t]:.0f} {'向上' if a.k[t] > a.k[t - 1] else '向下'}",
        "chips": chips_text(ch),
        "sector": f"{sec}（第 {rank} 強／{sec_rank[sec][1]} 類）" if rank else (sec or ""),
        "rs": rs,
        "basic_ok": bool(basic_ok),
        "setups": setups,
    }


def screen(df, last_date, short_ok=None, ctx=None):
    """short_ok：每個市場 {代號: 能不能融券}；某市場是 None 代表抓不到、不過濾。
    ctx：{"chips": 籌碼摘要, "industry": 產業對照, "held": 持股代號}，都可以沒有。"""
    ctx = ctx or {}
    chips, industry, held = ctx.get("chips") or {}, ctx.get("industry") or {}, set(ctx.get("held") or [])
    exclude = load_exclude()
    df = df.sort_values(["code", "date"])
    rs = rs_rank(df, last_date)
    sec_rank = sector_rank(df, last_date, industry)
    out = {"long": [], "short": [], "confirmed": [], "excluded": [], "watch": [], "records": {}, "analyses": {}}
    for code, g in df.groupby("code"):
        if g["date"].iloc[-1] != last_date or len(g) < config.MIN_BARS:
            continue  # 今天沒交易（停牌等）或資料太短就跳過
        last = g.iloc[-1]
        vol5 = g["volume"].tail(5).mean()
        basic_ok = code not in exclude and last["close"] >= config.MIN_PRICE and vol5 >= config.MIN_AVG_VOL_LOTS
        r = rs.get(code)
        try:
            a = analyze(g)
            res = evaluate(a, a.t, r)
            conf = confirmed_today(a, r) if basic_ok else []
        except Exception as e:  # noqa: BLE001
            print(f"{code} 判斷錯誤：{type(e).__name__} {e}")
            continue
        base = {"code": code, "name": last["name"], "market": last["market"]}
        ch, sec = chips.get(code), industry.get(code)
        table = (short_ok or {}).get(last["market"])
        can_short = None if table is None else bool(table.get(code, False))
        if res["short"] and can_short is False:
            res["excluded"].append({**res["short"], "why": [NO_SHORT]})
            res["short"] = None
        for x in [res["long"], res["short"]] + res["excluded"]:
            if x:
                add_context(x, x["side"], ch, sec, sec_rank)
        out["records"][code] = record(a, base, res, r, ch, sec, sec_rank, basic_ok)
        if code in held:
            out["analyses"][code] = a
        if not basic_ok:
            continue
        conf = [x for x in conf if not (x["side"] == "short" and can_short is False)]
        for slot in ("long", "short"):
            if res[slot]:
                out[slot].append({**base, **res[slot]})
        out["excluded"] += [{**base, **x} for x in res["excluded"]]
        out["confirmed"] += [{**base, **x} for x in conf]
        # 鎖股名單：當天漲跌 3.5% 以上、趨勢方向一致的強勢／弱勢股
        rec = out["records"][code]
        if (rec["chg"] >= 3.5 and a.trend[a.t] == 1) or (rec["chg"] <= -3.5 and a.trend[a.t] == -1):
            out["watch"].append({**base, "close": rec["close"], "chg": rec["chg"], "trend": rec["trend"],
                                 "ma": rec["ma"], "pos": rec["pos"], "vol": rec["vol"], "chips": rec["chips"],
                                 "sector": rec["sector"], "rs": r})
    # 今天收盤已經走出進場K的，不再列入明天的預備名單
    done = {(x["code"], x["side"]) for x in out["confirmed"]}
    for slot in ("long", "short"):
        out[slot] = [x for x in out[slot] if (x["code"], slot) not in done]
    for k in ("long", "short", "confirmed", "excluded"):
        out[k].sort(key=lambda x: (-x["score"], x["gap_pct"]))
    out["excluded"] = out["excluded"][:MAX_EXCLUDED]
    out["watch"].sort(key=lambda x: -abs(x["chg"]))
    out["watch"] = out["watch"][:MAX_WATCH]
    return out


def _n(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def message(res, mkt, last_date, short_ok=None, hold_lines=None):
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
            vol = (f"、量≥{r['need_vol']}倍" if r["need_vol"] > 1 else "、量要平常以上") if r["need_vol"] else ""
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
    lines += hold_lines or []
    url = line_push.dashboard_url()
    if url:
        lines.append(f"\n完整清單：{url}")
    lines.append("僅供參考，依紀律操作、自行判斷")
    return "\n".join(lines)


def write_records(records, last_date):
    """每檔股票的分析摘要，依代號第一碼分檔存到 docs/analysis/，給 LINE 查個股用。"""
    folder = os.path.join(os.path.dirname(OUT), "analysis")
    os.makedirs(folder, exist_ok=True)
    shards = {}
    for code, rec in records.items():
        shards.setdefault(code[0], {})[code] = rec
    for k, stocks in shards.items():
        with open(os.path.join(folder, f"{k}.json"), "w", encoding="utf-8") as f:
            json.dump({"date": last_date.strftime("%Y-%m-%d"), "stocks": stocks}, f, ensure_ascii=False,
                      separators=(",", ":"))


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

    # 籌碼（三大法人、融資融券）與產業分類：抓不到也不影響選股
    try:
        chips = data.update_chips(sorted(df["date"].dt.date.unique()))
    except Exception as e:  # noqa: BLE001
        print(f"籌碼資料有問題，這次不用籌碼：{e}")
        chips = None
    markets = df[df["date"] == last_date].set_index("code")["market"].to_dict()
    short_ok = data.shortable_from(chips, markets)
    try:
        industry = data.update_industry(today)
    except Exception as e:  # noqa: BLE001
        print(f"產業分類有問題，這次不排類股：{e}")
        industry = {}

    held = holdings.fetch()  # 沒設定持股功能 → None
    ctx = {"chips": data.chips_summary(chips), "industry": industry, "held": [h["code"] for h in held or []]}
    res = screen(df, last_date, short_ok, ctx)
    records, analyses = res.pop("records"), res.pop("analyses")
    write_records(records, last_date)

    if held is not None:
        advice = []
        for h in held:
            a = analyses.get(h["code"])
            if a is None:
                continue  # 今天沒交易（停牌等）
            try:
                advice.append(holdings.evaluate(a, h))
            except Exception as e:  # noqa: BLE001
                print(f"持股 {h['code']} 判斷錯誤：{type(e).__name__} {e}")
        holdings.save_advice(advice)  # 輸入「持股」時顯示；晚上推播不放持股

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
    try:
        journal.nightly(payload, df)  # 寫進你的 Google 試算表（選股紀錄＋事後結果）
    except Exception as e:  # noqa: BLE001
        print(f"紀錄表更新失敗（不影響選股）：{type(e).__name__} {e}")
    if not line_push.push(message(res, mkt, last_date, short_ok)):
        sys.exit(1)  # 讓 GitHub 顯示紅叉提醒你，但資料已經存好了


if __name__ == "__main__":
    main()
