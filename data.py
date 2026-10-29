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
OTC_REFILL_MAX = 15  # 每次執行最多補抓幾天缺漏的上櫃資料

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
        r = requests.post(url, data=params, headers=HEADERS, timeout=60)
    else:
        r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    j = r.json()
    for t in j.get("tables") or []:
        fields = t.get("fields") or []
        if _find_col(fields, "代號") is not None and _find_col(fields, "收盤") is not None:
            return _rows_from_table(fields, t.get("data") or [], day, "otc")
    return []


def _tpex_old(day):
    roc = f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"
    url = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
    j = requests.get(url, params={"l": "zh-tw", "d": roc, "se": "EW"}, headers=HEADERS, timeout=60).json()
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
        time.sleep(SLEEP * (attempt + 2))  # 越失敗等越久
    print(f"{day} 上櫃資料抓取失敗（{', '.join(errors[-3:])}），下次執行會再補抓")
    return []


def fetch_day(day: dt.date):
    """抓某一天上市＋上櫃。回傳 DataFrame；休市回傳空表。"""
    rows = fetch_twse(day)
    time.sleep(SLEEP)
    if not rows:  # 上市沒資料 = 休市，上櫃就不用抓了
        return pd.DataFrame(columns=COLS)
    rows += fetch_tpex(day)
    time.sleep(SLEEP)
    return _frame(rows)


def _frame(rows):
    df = pd.DataFrame(rows, columns=COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df


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
    更新資料到 today。第一次執行會往回補約 HISTORY_DAYS 個交易日（約 20–30 分鐘）。
    回傳 (完整資料, today 是否為交易日)
    """
    df = load()
    have = set(pd.to_datetime(df["date"]).dt.date) if len(df) else set()

    # 要補的日期：從最舊需要的日子到 today，跳過週末與已經有的日子
    if len(have) >= config.HISTORY_DAYS * 0.9:
        start = max(have) + dt.timedelta(days=1)
    else:
        start = today - dt.timedelta(days=int(config.HISTORY_DAYS * 1.5) + 10)
        print(f"歷史資料不足，開始回補（從 {start} 起），第一次會比較久……")

    new = []
    d = start
    while d <= today:
        if d.weekday() < 5 and d not in have:
            try:
                part = fetch_day(d)
                if len(part):
                    n_otc = (part["market"] == "otc").sum()
                    print(f"{d} 取得 {len(part)} 檔（上櫃 {n_otc}）")
                    new.append(part)
            except Exception as e:  # noqa: BLE001
                print(f"{d} 抓取失敗：{e}")
                time.sleep(SLEEP * 3)
        d += dt.timedelta(days=1)

    # 補抓：有上市資料、但上櫃缺的日子（最近的優先，每次最多補 OTC_REFILL_MAX 天）
    if len(df):
        otc_days = set(df.loc[df["market"] == "otc", "date"].dt.date)
        missing = sorted((x for x in have if x not in otc_days), reverse=True)[:OTC_REFILL_MAX]
        for d in missing:
            rows = fetch_tpex(d, tries=1)
            time.sleep(SLEEP)
            if rows:
                print(f"{d} 補抓上櫃 {len(rows)} 檔")
                new.append(_frame(rows))

    if new:
        df = pd.concat([df] + new, ignore_index=True)
        df = df.drop_duplicates(["date", "code"], keep="last")
        save(df)
        df = load()

    traded_today = len(df) > 0 and today in set(pd.to_datetime(df["date"]).dt.date)
    return df, traded_today
