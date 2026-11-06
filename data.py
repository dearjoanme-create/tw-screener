"""抓取上市（證交所）＋上櫃（櫃買中心）全市場日行情，並存成歷史資料檔。"""
import datetime as dt
import os
import re
import time

import pandas as pd
import requests

import config

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "prices.csv.gz")
HEADERS = {"User-Agent": "Mozilla/5.0 (tw-screener)"}
SLEEP = 3.5  # 證交所有流量限制，太快會被暫時封鎖 IP
TPEX_TIMEOUT = 20    # 櫃買中心每次連線最多等幾秒
SAVE_EVERY = 20      # 回補時每抓幾天就存檔一次
YAHOO_BATCH = 100    # Yahoo 每批抓幾檔

COLS = ["date", "code", "name", "market", "open", "high", "low", "close", "volume"]


def _num(s):
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "---", "----", "X", "除權息", "除權", "除息"):
        return None
    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _find_col(fields, *keywords, exclude=()):
    """在欄位名稱中找出包含關鍵字的那一欄，回傳索引。"""
    for i, f in enumerate(fields):
        f = str(f).replace(" ", "")
        if any(k in f for k in keywords) and not any(x in f for x in exclude):
            return i
    return None


def _is_common_stock(code):
    # 4 碼數字、不是 00 開頭（ETF）→ 一般股票
    return bool(re.fullmatch(r"[1-9]\d{3}", code))


def _rows_from_table(fields, data, day, market):
    ic = _find_col(fields, "證券代號", "代號")
    iname = _find_col(fields, "證券名稱", "名稱")
    io = _find_col(fields, "開盤")
    ih = _find_col(fields, "最高")
    il = _find_col(fields, "最低")
    iclose = _find_col(fields, "收盤")
    iv = _find_col(fields, "成交股數")
    if None in (ic, iname, io, ih, il, iclose, iv):
        return []
    rows = []
    for r in data:
        code = str(r[ic]).strip()
        if not _is_common_stock(code):
            continue
        o, h, l, c, v = (_num(r[i]) for i in (io, ih, il, iclose, iv))
        if None in (o, h, l, c) or not v:
            continue  # 當天沒成交
        rows.append([day, code, str(r[iname]).strip(), market, o, h, l, c, v / 1000.0])
    return rows


def fetch_twse(day: dt.date):
    """上市：證交所每日收盤行情（全部，不含權證/牛熊證）。"""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": day.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"}
    j = requests.get(url, params=params, headers=HEADERS, timeout=30).json()
    if j.get("stat") != "OK":
        return []
    tables = j.get("tables") or []
    # 舊格式相容：fields9 / data9
    if not tables and "fields9" in j:
        tables = [{"fields": j["fields9"], "data": j["data9"]}]
    for t in tables:
        fields = t.get("fields") or []
        if _find_col(fields, "證券代號") is not None and _find_col(fields, "收盤") is not None:
            return _rows_from_table(fields, t.get("data") or [], day, "tse")
    return []


def _tpex_new(day, method):
    url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQ"
    params = {"date": day.strftime("%Y/%m/%d"), "type": "EW", "response": "json"}
    if method == "post":
        r = requests.post(url, data=params, headers=HEADERS, timeout=TPEX_TIMEOUT)
    else:
        r = requests.get(url, params=params, headers=HEADERS, timeout=TPEX_TIMEOUT)
    j = r.json()
    for t in j.get("tables") or []:
        fields = t.get("fields") or []
        if _find_col(fields, "代號") is not None and _find_col(fields, "收盤") is not None:
            return _rows_from_table(fields, t.get("data") or [], day, "otc")
    return []


def _tpex_old(day):
    roc = f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"
    url = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
    j = requests.get(url, params={"l": "zh-tw", "d": roc, "se": "EW"}, headers=HEADERS, timeout=TPEX_TIMEOUT).json()
    data = j.get("aaData") or []
    if not data and j.get("tables"):
        t = j["tables"][0]
        return _rows_from_table(t.get("fields") or [], t.get("data") or [], day, "otc")
    # 舊版固定欄位：代號,名稱,收盤,漲跌,開盤,最高,最低,均價,成交股數,...
    fields = ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數"]
    return _rows_from_table(fields, [r[:9] for r in data], day, "otc")


def fetch_tpex(day: dt.date, tries=3):
    """上櫃：櫃買中心每日收盤行情。新版（POST/GET）、舊版輪流試，最多 tries 輪。"""
    errors = []
    for attempt in range(tries):
        for name, fn in (("新版POST", lambda: _tpex_new(day, "post")),
                         ("舊版", lambda: _tpex_old(day)),
                         ("新版GET", lambda: _tpex_new(day, "get"))):
            try:
                rows = fn()
                if rows:
                    return rows
                errors.append(f"{name}:無資料")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}:{type(e).__name__}")
            time.sleep(2)
        if attempt + 1 < tries:
            time.sleep(SLEEP * (attempt + 2))  # 越失敗等越久
    print(f"{day} 上櫃資料抓取失敗（{', '.join(errors[-3:])}），下次執行會再補抓")
    return []


def fetch_day(day: dt.date, otc_tries=3, skip_otc=False):
    """抓某一天上市＋上櫃。回傳 DataFrame；休市回傳空表。"""
    rows = fetch_twse(day)
    time.sleep(SLEEP)
    if not rows:  # 上市沒資料 = 休市，上櫃就不用抓了
        return pd.DataFrame(columns=COLS)
    if not skip_otc:
        rows += fetch_tpex(day, tries=otc_tries)
        time.sleep(SLEEP)
    return _frame(rows)


def _frame(rows):
    df = pd.DataFrame(rows, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _otc_sig(rows_or_df):
    """一天上櫃資料的「指紋」：用來發現櫃買中心回傳的其實是別天的資料。"""
    if isinstance(rows_or_df, pd.DataFrame):
        t = rows_or_df[["code", "close", "volume"]].itertuples(index=False, name=None)
    else:
        t = ((r[1], r[7], r[8]) for r in rows_or_df)
    return hash(tuple(sorted((c, round(float(p), 2), round(float(v), 1)) for c, p, v in t)))


def purge_bad_otc(df):
    """刪掉『每天都一樣』的上櫃資料（舊版程式抓錯的），之後會改用 Yahoo 補回正確的。"""
    otc = df[df["market"] == "otc"]
    if otc.empty:
        return df
    sigs = otc.groupby("date").apply(_otc_sig)
    dup_dates = set(sigs[sigs.duplicated(keep=False)].index)
    if dup_dates:
        print(f"發現 {len(dup_dates)} 天的上櫃資料重複（日期錯誤），先刪除，稍後用 Yahoo 補正確資料")
        df = df[~((df["market"] == "otc") & (df["date"].isin(dup_dates)))]
    return df


def fetch_yahoo_otc(codes_names, days):
    """用 Yahoo 抓上櫃股票在指定日期的日K。codes_names: {代號: 名稱}，days: 日期集合"""
    import yfinance as yf

    if not codes_names or not days:
        return pd.DataFrame(columns=COLS)
    start = min(days) - dt.timedelta(days=3)
    end = max(days) + dt.timedelta(days=1)
    codes = sorted(codes_names)
    out = []
    for i in range(0, len(codes), YAHOO_BATCH):
        batch = codes[i:i + YAHOO_BATCH]
        tickers = [f"{c}.TWO" for c in batch]
        try:
            raw = yf.download(tickers, start=start.isoformat(), end=end.isoformat(), interval="1d",
                              group_by="ticker", auto_adjust=False, threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            print(f"Yahoo 第 {i // YAHOO_BATCH + 1} 批失敗：{e}")
            time.sleep(10)
            continue
        if raw is None or raw.empty:
            continue
        for c, t in zip(batch, tickers):
            try:
                sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
            sub = sub[sub["Volume"] > 0]
            for ts, r in sub.iterrows():
                d = pd.Timestamp(ts).date()
                if d in days:
                    out.append([d, c, codes_names[c], "otc", float(r["Open"]), float(r["High"]),
                                float(r["Low"]), float(r["Close"]), float(r["Volume"]) / 1000.0])
        print(f"Yahoo 上櫃 {min(i + YAHOO_BATCH, len(codes))}/{len(codes)} 檔")
        time.sleep(2)
    return _frame(out) if out else pd.DataFrame(columns=COLS)


# ------------------------------------------------------------------
# 可不可以融券（做空用）
# ------------------------------------------------------------------
STOP_SHORT_MARKS = ("X", "停券", "停止融券")  # 證交所註記：O 停止融資、X 停止融券


def _margin_table(fields, data):
    """融資融券餘額表 → {代號: 可不可以融券}。
    不在表裡＝不是信用交易標的；融券限額是 0 或註記停止融券＝不能融券。"""
    ic = _find_col(fields, "代號")
    if ic is None:
        return {}
    names = [str(f).replace(" ", "") for f in fields]
    lim = [i for i, f in enumerate(names) if "券限額" in f]
    if not lim:  # 證交所：融資、融券各有一欄「次一營業日限額」，第二個是融券
        lim = [i for i, f in enumerate(names) if "限額" in f][1:2]
    note = _find_col(fields, "註記", "備註")
    out = {}
    for r in data:
        code = str(r[ic]).strip()
        if not _is_common_stock(code):
            continue
        ok = True
        if lim and len(r) > lim[0]:
            v = _num(r[lim[0]])
            if v is not None and v <= 0:
                ok = False
        if note is not None and len(r) > note and any(m in str(r[note]) for m in STOP_SHORT_MARKS):
            ok = False
        out[code] = ok
    return out


def fetch_margin_twse(day: dt.date):
    url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
    params = {"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"}
    j = requests.get(url, params=params, headers=HEADERS, timeout=30).json()
    if j.get("stat") not in (None, "OK"):
        return {}
    for t in j.get("tables") or [{"fields": j.get("fields"), "data": j.get("data")}]:
        fields = t.get("fields") or []
        if _find_col(fields, "代號") is not None and len(t.get("data") or []) > 50:
            return _margin_table(fields, t["data"])
    return {}


def fetch_margin_tpex(day: dt.date):
    tries = [
        ("get", "https://www.tpex.org.tw/www/zh-tw/margin/balance",
         {"date": day.strftime("%Y/%m/%d"), "response": "json"}),
        ("get", "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php",
         {"l": "zh-tw", "o": "json", "d": f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"}),
    ]
    for _, url, params in tries:
        try:
            j = requests.get(url, params=params, headers=HEADERS, timeout=TPEX_TIMEOUT).json()
        except Exception:  # noqa: BLE001
            continue
        for t in j.get("tables") or []:
            fields = t.get("fields") or []
            if _find_col(fields, "代號") is not None and len(t.get("data") or []) > 50:
                return _margin_table(fields, t["data"])
        data = j.get("aaData") or []
        if len(data) > 50:  # 舊版固定欄位：代號,名稱,前資餘額,資買,資賣,現償,資餘額,資屬證金,資使用率,資限額,
            fields = ["代號", "名稱", "前資餘額", "資買", "資賣", "現償", "資餘額", "資屬證金", "資使用率", "資限額",
                      "前券餘額", "券賣", "券買", "券償", "券餘額", "券屬證金", "券使用率", "券限額", "資券相抵", "備註"]
            return _margin_table(fields, data)
        time.sleep(2)
    return {}


def shortable(last_date: dt.date, lookback=6):
    """回傳 {代號: True/False}；某個市場抓不到就是 None（不過濾，請自行確認）。
    晚上掃描時當天的融資融券可能還沒公布，往前找最近一天的資料（標的很少變動）。"""
    out = {"tse": None, "otc": None}
    for market, fn in (("tse", fetch_margin_twse), ("otc", fetch_margin_tpex)):
        for back in range(lookback):
            d = last_date - dt.timedelta(days=back)
            if d.weekday() >= 5:
                continue
            try:
                got = fn(d)
            except Exception as e:  # noqa: BLE001
                print(f"{d} {market} 融資融券抓取失敗：{type(e).__name__}")
                got = {}
            time.sleep(SLEEP)
            if got:
                out[market] = got
                break
        if out[market] is None:
            print(f"{market} 融資融券資料抓不到，這次放空不過濾，請自行確認能否融券")
    return out


# ------------------------------------------------------------------
# 加權指數（判斷大盤多空、資金水位用）
# ------------------------------------------------------------------
INDEX_FILE = os.path.join(os.path.dirname(__file__), "data", "taiex.csv")
INDEX_COLS = ["date", "open", "high", "low", "close"]


def _roc_date(s):
    """'115/11/04' → date(2026, 11, 4)"""
    y, m, d = (int(x) for x in str(s).strip().split("/"))
    return dt.date(y + 1911 if y < 1911 else y, m, d)


def fetch_index_month(month: dt.date):
    """證交所：某個月每天的加權指數開高低收。"""
    url = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
    params = {"date": month.strftime("%Y%m01"), "response": "json"}
    j = requests.get(url, params=params, headers=HEADERS, timeout=30).json()
    if j.get("stat") != "OK":
        return []
    fields = j.get("fields") or []
    idx = [_find_col(fields, "日期"), _find_col(fields, "開盤"), _find_col(fields, "最高"),
           _find_col(fields, "最低"), _find_col(fields, "收盤")]
    if None in idx:
        return []
    rows = []
    for r in j.get("data") or []:
        vals = [_num(r[i]) for i in idx[1:]]
        if None in vals:
            continue
        rows.append([_roc_date(r[idx[0]])] + vals)
    return rows


def fetch_index_yahoo(start: dt.date, end: dt.date):
    import yfinance as yf

    raw = yf.download("^TWII", start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(),
                      interval="1d", auto_adjust=False, progress=False)
    if raw is None or raw.empty:
        return []
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.dropna(subset=["Open", "High", "Low", "Close"])
    return [[pd.Timestamp(ts).date(), float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])]
            for ts, r in raw.iterrows()]


def update_index(today: dt.date):
    """更新加權指數到 today。只重抓這個月和上個月，其他月份用存檔。抓不到就用 Yahoo。"""
    old = pd.DataFrame(columns=INDEX_COLS)
    if os.path.exists(INDEX_FILE):
        old = pd.read_csv(INDEX_FILE, parse_dates=["date"])
    have_months = set(old["date"].dt.strftime("%Y%m")) if len(old) else set()
    start = today - dt.timedelta(days=int(config.HISTORY_DAYS * 1.5) + 10)
    months, m = [], dt.date(start.year, start.month, 1)
    while m <= today:
        months.append(m)
        m = dt.date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    recent = {months[-1].strftime("%Y%m"), months[-2].strftime("%Y%m") if len(months) > 1 else ""}
    todo = [m for m in months if m.strftime("%Y%m") not in have_months or m.strftime("%Y%m") in recent]
    rows, failed = [], False
    for m in todo:
        try:
            got = fetch_index_month(m)
            if not got and m.strftime("%Y%m") not in have_months:
                failed = True
            rows += got
        except Exception as e:  # noqa: BLE001
            print(f"加權指數 {m:%Y-%m} 抓取失敗：{type(e).__name__}")
            failed = True
        time.sleep(SLEEP)
    if failed:
        try:
            rows += fetch_index_yahoo(start, today)
            print("加權指數改用 Yahoo 補")
        except Exception as e:  # noqa: BLE001
            print(f"加權指數 Yahoo 也失敗：{e}")
    new = pd.DataFrame(rows, columns=INDEX_COLS)
    if len(new):
        new["date"] = pd.to_datetime(new["date"])
    df = pd.concat([old, new], ignore_index=True) if len(old) else new
    if not len(df):
        return df
    df = df.drop_duplicates("date", keep="last").sort_values("date")
    df = df[df["date"] >= pd.Timestamp(start)]
    os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
    df.to_csv(INDEX_FILE, index=False, float_format="%.2f")
    return df.reset_index(drop=True)


def load():
    if os.path.exists(DATA_FILE):
        df = pd.read_csv(DATA_FILE, dtype={"code": str}, parse_dates=["date"])
        return df
    return pd.DataFrame(columns=COLS)


def save(df):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    dates = sorted(df["date"].unique())[-config.HISTORY_DAYS:]
    df = df[df["date"].isin(dates)].sort_values(["code", "date"])
    df.to_csv(DATA_FILE, index=False, compression="gzip", float_format="%.4g")


def update(today: dt.date):
    """
    更新資料到 today。
    - 上市：證交所，每天一筆（第一次回補約 HISTORY_DAYS 天，每 SAVE_EVERY 天存檔一次）
    - 上櫃：當天的用櫃買中心（它只給得出最新一天）；歷史和缺漏的日子用 Yahoo 補
    回傳 (完整資料, today 是否為交易日)
    """
    df = load()
    before = len(df)
    # 先記下上櫃名單（就算價格是錯的，代號和名稱還是對的）
    otc_names = dict(zip(df.loc[df["market"] == "otc", "code"], df.loc[df["market"] == "otc", "name"]))
    df = purge_bad_otc(df)
    if len(df) != before:
        save(df)
        df = load()
    have = set(df["date"].dt.date) if len(df) else set()

    backfill = len(have) < config.HISTORY_DAYS * 0.9
    if backfill:
        start = today - dt.timedelta(days=int(config.HISTORY_DAYS * 1.5) + 10)
        print(f"歷史資料不足（目前 {len(have)} 天），開始回補（從 {start} 起）……")
    else:
        start = max(have) + dt.timedelta(days=1)

    todo = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
    todo = [d for d in todo if d.weekday() < 5 and d not in have]
    print(f"上市需要抓 {len(todo)} 個工作日")

    new, unsaved = [], 0

    def flush():
        nonlocal df, new, unsaved
        if new:
            df = pd.concat([df] + new, ignore_index=True)
            df = df.drop_duplicates(["date", "code"], keep="last")
            save(df)
            df = load()
            print(f"—— 已存檔，目前共 {df['date'].nunique()} 個交易日 ——")
        new, unsaved = [], 0

    for i, d in enumerate(todo, 1):
        try:
            # 櫃買中心只抓得到最新一天，所以只有「今天」才向它要上櫃資料
            part = fetch_day(d, otc_tries=3, skip_otc=(d != today))
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(todo)}] {d} 抓取失敗：{e}")
            time.sleep(SLEEP * 3)
            continue
        if not len(part):
            print(f"[{i}/{len(todo)}] {d} 休市")
            continue
        n_otc = int((part["market"] == "otc").sum())
        print(f"[{i}/{len(todo)}] {d} 上市 {len(part) - n_otc} 檔" + (f"、上櫃 {n_otc} 檔" if n_otc else ""))
        new.append(part)
        unsaved += 1
        if unsaved >= SAVE_EVERY:
            flush()
    flush()

    # 今天的上櫃資料如果跟前一天一模一樣，代表櫃買中心給的是舊資料，丟掉改用 Yahoo
    if len(df):
        otc = df[df["market"] == "otc"]
        dates = sorted(otc["date"].unique())
        if len(dates) >= 2 and pd.Timestamp(today) == dates[-1]:
            if _otc_sig(otc[otc["date"] == dates[-1]]) == _otc_sig(otc[otc["date"] == dates[-2]]):
                print("今天櫃買中心給的上櫃資料跟前一天一樣，丟掉改用 Yahoo")
                df = df[~((df["market"] == "otc") & (df["date"] == dates[-1]))]
                save(df)
                df = load()

    # 上櫃缺漏的日子：一次用 Yahoo 補
    if len(df):
        have = set(df["date"].dt.date)
        otc = df[df["market"] == "otc"]
        missing = have - set(otc["date"].dt.date)
        if missing:
            names = {**otc_names, **dict(zip(otc["code"], otc["name"]))}
            if not names:  # 完全沒有上櫃名單：向櫃買中心要一次最新清單（只用代號和名稱）
                rows = fetch_tpex(today, tries=2)
                names = {r[1]: r[2] for r in rows}
            print(f"有 {len(missing)} 天缺上櫃資料，用 Yahoo 補 {len(names)} 檔……")
            part = fetch_yahoo_otc(names, missing)
            if len(part):
                print(f"Yahoo 補到 {part['date'].nunique()} 天、{part['code'].nunique()} 檔")
                new.append(part)
                flush()
            else:
                print("Yahoo 這次沒有補到資料，明天會再試")

    traded_today = len(df) > 0 and today in set(df["date"].dt.date)
    return df, traded_today
