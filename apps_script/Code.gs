/**
 * 台股趨勢選股 LINE 小幫手（Google Apps Script）
 *
 * LINE 指令：
 *   2317            查個股分析（前一天收盤的結果）
 *   買 2317 210     記錄做多（可加 短／長／飆，預設短線），例：買 2317 210 長
 *   空 2317 210     記錄放空
 *   賣 2317 230     平倉（不寫價格就用目前價格）
 *   刪除 2317       刪掉打錯的持股紀錄（不算績效）
 *   持股            目前持股、損益、停損、建議
 *   績效            已平倉的勝率、平均賺賠
 *   說明            指令說明
 *   ID              回覆自己的 User ID（任何人都能用，給朋友加入 13:00 通知）
 *
 * 指令碼屬性（專案設定 → 指令碼屬性）：
 *   LINE_TOKEN    LINE Channel access token（跟 GitHub 的 LINE_CHANNEL_ACCESS_TOKEN 一樣）
 *   LINE_USER_ID  你的 LINE User ID（跟 GitHub 的 LINE_USER_ID 一樣；只有你能使用）
 *   SECRET        自己取一串英文數字當密碼（跟 GitHub 的 HOLDINGS_SECRET 一樣）
 *   PAGES_URL     你的選股網頁，例：https://dearjoanme-create.github.io/tw-screener/
 */

var STOP_PCT = 0.05;
var HOLD_SHEET = '持股';
var ADVICE_SHEET = '建議';
var HOLD_HEAD = ['id', 'code', 'name', 'market', 'side', 'price', 'date', 'mode', 'status',
                 'exit_price', 'exit_date', 'pnl', 'created'];
var MODE_NAME = { '短': '短線', '長': '長線', '飆': '飆股' };
var ICON = { '續抱': '🟢', '警示': '🟡', '減碼': '🟠', '出場': '🔴' };

function prop(k) { return PropertiesService.getScriptProperties().getProperty(k) || ''; }

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ------------------------------------------------------------------
// 進入點
// ------------------------------------------------------------------
function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.action === 'holdings') {
    if (!prop('SECRET') || p.secret !== prop('SECRET')) return json({ ok: false, error: 'secret 不對' });
    return json({ ok: true, holdings: openHoldings(true) });
  }
  return ContentService.createTextOutput('OK');
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse(e.postData.contents); } catch (err) { return json({ ok: false }); }

  if (body.action === 'advice') {  // GitHub 每天晚上送來的持股建議
    if (!prop('SECRET') || body.secret !== prop('SECRET')) return json({ ok: false, error: 'secret 不對' });
    saveAdvice(body.items || []);
    return json({ ok: true });
  }

  (body.events || []).forEach(function (ev) {
    if (ev.type !== 'message' || !ev.message || ev.message.type !== 'text') return;
    var owner = prop('LINE_USER_ID');
    var text;
    var uid = (ev.source && ev.source.userId) || '';
    if (/^(id|我的id)$/i.test(normalize(ev.message.text))) {  // 任何人都能查自己的 User ID
      text = '你的 User ID：\n' + uid + '\n\n把這串傳給帳號主人，就能收到 13:00 的收盤前確認通知。';
    } else if (owner && uid !== owner) {
      text = '這是私人帳號，無法使用。';
    } else {
      try { text = handle(ev.message.text); } catch (err) { text = '出錯了：' + err; }
    }
    reply(ev.replyToken, text);
  });
  return json({ ok: true });
}

function reply(token, text) {
  UrlFetchApp.fetch('https://api.line.me/v2/bot/message/reply', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + prop('LINE_TOKEN') },
    payload: JSON.stringify({ replyToken: token, messages: [{ type: 'text', text: String(text).slice(0, 4900) }] }),
    muteHttpExceptions: true
  });
}

// ------------------------------------------------------------------
// 指令
// ------------------------------------------------------------------
function normalize(s) {
  return String(s || '')
    .replace(/[０-９．]/g, function (ch) { return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0); })
    .replace(/　/g, ' ').replace(/\s+/g, ' ').trim();
}

function handle(raw) {
  var s = normalize(raw), m;
  if (/^(說明|help|指令|\?|？)$/i.test(s)) return helpText();
  if ((m = s.match(/^(\d{4})$/))) return queryStock(m[1]);
  if ((m = s.match(/^(買|多|做多|空|放空)\s*(\d{4})\s+(\d+(?:\.\d+)?)\s*(短|長|飆)?$/)))
    return addHolding(m[2], /空/.test(m[1]) ? 'short' : 'long', Number(m[3]), m[4] || '短');
  if ((m = s.match(/^(賣|回補|平倉)\s*(\d{4})(?:\s+(\d+(?:\.\d+)?))?$/))) return closeHolding(m[2], m[3] ? Number(m[3]) : null);
  if ((m = s.match(/^刪除\s*(\d{4})$/))) return deleteHolding(m[1]);
  if (s === '持股') return holdingsText();
  if (s === '績效') return performanceText();
  return '看不懂這個指令。\n\n' + helpText();
}

function helpText() {
  return [
    '📖 可以用的指令',
    '2317　查個股分析',
    '買 2317 210　記錄做多（預設短線）',
    '買 2317 210 長　用長線守則（飆＝飆股）',
    '空 2317 210　記錄放空',
    '賣 2317 230　平倉（不寫價格用目前價格）',
    '刪除 2317　刪掉打錯的紀錄',
    '持股　目前持股和建議',
    '績效　勝率、平均賺賠'
  ].join('\n');
}

// ------------------------------------------------------------------
// 個股資料（GitHub 每晚算好的 docs/analysis/）
// ------------------------------------------------------------------
function stockRecord(code) {
  var base = prop('PAGES_URL').replace(/\/?$/, '/');
  var res = UrlFetchApp.fetch(base + 'analysis/' + code.charAt(0) + '.json?t=' + Date.now(), { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) return null;
  var data = JSON.parse(res.getContentText());
  return (data.stocks || {})[code] || null;
}

function liveQuotes(list) {  // list: [{code, market}]
  var out = {};
  if (!list.length) return out;
  try {
    var exch = list.map(function (x) { return (x.market === 'otc' ? 'otc_' : 'tse_') + x.code + '.tw'; }).join('|');
    var res = UrlFetchApp.fetch('https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch=' +
      encodeURIComponent(exch), { muteHttpExceptions: true, headers: { Referer: 'https://mis.twse.com.tw/stock/index.jsp' } });
    var arr = JSON.parse(res.getContentText()).msgArray || [];
    arr.forEach(function (q) {
      var p = parseFloat(String(q.z).split('_')[0]) || parseFloat(String(q.b).split('_')[0]) || parseFloat(String(q.a).split('_')[0]);
      if (p > 0) out[q.c] = { price: p, time: q.t, date: q.d };
    });
  } catch (err) { /* 抓不到就用收盤價 */ }
  return out;
}

function chartUrl(code, market) {
  return 'https://tw.stock.yahoo.com/quote/' + code + (market === 'otc' ? '.TWO' : '.TW') + '/technical-analysis';
}

function fmt(x) { return (Math.round(Number(x) * 100) / 100).toString(); }
function pct(x) { return (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%'; }

function queryStock(code) {
  var r = stockRecord(code);
  if (!r) return '找不到 ' + code + '（可能不是上市櫃普通股，或資料太短）。';
  var d = r.date.slice(5).replace('-', '/');
  var lines = [
    code + ' ' + r.name + '｜' + d + ' 收盤 ' + fmt(r.close) + '（' + (r.chg >= 0 ? '+' : '') + r.chg + '%）',
    '趨勢：' + r.trend,
    '均線：' + r.ma,
    '位階：' + r.pos,
    '成交量：' + r.vol,
    'KD：' + r.kd
  ];
  if (r.chips) lines.push('籌碼：' + r.chips);
  if (r.sector) lines.push('類股：' + r.sector);
  if (r.rs != null) lines.push('強度：' + r.rs + '（全市場百分位，100 最強）');
  lines.push('—');
  if (!r.setups || !r.setups.length) {
    lines.push('目前沒有進場位置，先觀察');
  } else {
    r.setups.forEach(function (x) {
      var side = x.side === 'long' ? '做多' : '放空';
      if (x.why && x.why.length) {
        lines.push('⛔ ' + x.title + '（' + side + '）被排除：' + x.why.join('；'));
      } else {
        lines.push('✅ ' + x.title + '（' + side + '）');
        lines.push('  明天收盤要 ' + (x.side === 'long' ? '≥ ' : '≤ ') + fmt(x.trigger) +
          (x.need_vol ? (x.need_vol > 1 ? '、量 ≥ 平常的 ' + x.need_vol + ' 倍' : '、量要平常以上') : '') + '，停損 ' + fmt(x.stop));
      }
    });
    if (!r.basic_ok) lines.push('（股價或成交量太小，不在每日選股範圍）');
  }
  var mine = openHoldings(true).filter(function (h) { return h.code === code; });
  mine.forEach(function (h) {
    var g = gainOf(h, r.close);
    lines.push('—', '📋 你的持股：' + (h.side === 'long' ? '做多' : '放空') + ' 成本 ' + fmt(h.price) + '（' + pct(g) + '）');
    if (h.advice && h.advice.text) lines.push((ICON[h.advice.level] || '') + h.advice.level + '：' + h.advice.text);
  });
  lines.push('—', '線圖：' + chartUrl(code, r.market));
  return lines.join('\n');
}

// ------------------------------------------------------------------
// 持股（Google 試算表）
// ------------------------------------------------------------------
function sheet(name, head) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(head);
    sh.setFrozenRows(1);
  }
  return sh;
}

function rows(name, head) {
  var sh = sheet(name, head);
  var v = sh.getDataRange().getValues();
  var keys = v[0];
  return v.slice(1).map(function (r, i) {
    var o = { _row: i + 2 };
    keys.forEach(function (k, j) { o[k] = r[j]; });
    return o;
  });
}

function dateStr(x) {
  if (x instanceof Date) return Utilities.formatDate(x, 'Asia/Taipei', 'yyyy-MM-dd');
  return String(x || '').slice(0, 10);
}

function adviceMap() {
  var out = {};
  rows(ADVICE_SHEET, ['code', 'side', 'json', 'updated']).forEach(function (r) {
    try { out[String(r.code) + r.side] = JSON.parse(r.json); } catch (err) { /* 略過 */ }
  });
  return out;
}

function openHoldings(withAdvice) {
  var adv = withAdvice ? adviceMap() : {};
  return rows(HOLD_SHEET, HOLD_HEAD).filter(function (r) { return r.status === 'open'; }).map(function (r) {
    var code = String(r.code).replace(/^'/, '');
    return {
      code: code, name: r.name, market: r.market, side: r.side, price: Number(r.price),
      date: dateStr(r.date), mode: r.mode || '短', advice: adv[code + r.side] || null, _row: r._row
    };
  });
}

function gainOf(h, price) {
  return h.side === 'long' ? price / h.price - 1 : (h.price - price) / h.price;
}

function stopOf(h) {
  if (h.advice && h.advice.stop) return Number(h.advice.stop);
  return h.side === 'long' ? h.price * (1 - STOP_PCT) : h.price * (1 + STOP_PCT);
}

function today() { return Utilities.formatDate(new Date(), 'Asia/Taipei', 'yyyy-MM-dd'); }

function addHolding(code, side, price, mode) {
  var r = stockRecord(code);
  if (!r) return '找不到 ' + code + '，請確認代號。';
  var dup = openHoldings(false).filter(function (h) { return h.code === code && h.side === side; });
  if (dup.length) return '已經有 ' + code + ' 的' + (side === 'long' ? '做多' : '放空') + '紀錄了。要改的話先輸入「刪除 ' + code + '」再重新記錄。';
  if (Math.abs(price / r.close - 1) > 0.15) return '價格 ' + price + ' 跟最近收盤 ' + r.close + ' 差太多，請確認有沒有打錯。';
  var lock = LockService.getScriptLock(); lock.waitLock(10000);
  try {
    sheet(HOLD_SHEET, HOLD_HEAD).appendRow([Utilities.getUuid().slice(0, 8), "'" + code, r.name, r.market, side, price,
      today(), mode, 'open', '', '', '', new Date()]);
  } finally { lock.releaseLock(); }
  var stop = side === 'long' ? price * (1 - STOP_PCT) : price * (1 + STOP_PCT);
  var rules;
  if (side === 'short') {
    rules = ['獲利未達 5%，站上 5 日線續抱', '獲利超過 5%，收盤站上 5 日線停利回補', '出現底底高（趨勢改變）就回補'];
  } else if (mode === '長') {
    rules = ['沒跌破月線就續抱', '漲幅超過 10%，收盤跌破月線停利', '出現頭頭低（趨勢改變）就出場'];
  } else if (mode === '飆') {
    rules = ['沒破前 2 日低、沒破 3 日均線、沒出黑K就續抱', '量大增、開高走低出黑K就賣出'];
  } else {
    rules = ['漲幅未達 10%，跌破 5 日線續抱', '漲幅超過 10%，收盤跌破 5 日線停利', '出現頭頭低（趨勢改變）就出場'];
  }
  return [
    '✅ 已記錄：' + (side === 'long' ? '做多 ' : '放空 ') + code + ' ' + r.name + ' ' + fmt(price) + '（' + MODE_NAME[mode] + '）',
    '停損：' + fmt(stop) + '（收盤' + (side === 'long' ? '跌破' : '突破') + '就出場，設好不改）',
    '守則：', '・' + rules.join('\n・'),
    '每天晚上和 13:00 會幫你檢查，也可以隨時輸入「持股」查看。'
  ].join('\n');
}

function closeHolding(code, price) {
  var list = openHoldings(false).filter(function (h) { return h.code === code; });
  if (!list.length) return '沒有 ' + code + ' 的持股紀錄。';
  var h = list[0];
  if (price == null) {
    var q = liveQuotes([h])[code];
    if (q) price = q.price;
    else { var r = stockRecord(code); price = r ? r.close : null; }
  }
  if (!price) return '抓不到 ' + code + ' 目前價格，請輸入「賣 ' + code + ' 價格」。';
  var pnl = gainOf(h, price);
  var lock = LockService.getScriptLock(); lock.waitLock(10000);
  try {
    var sh = sheet(HOLD_SHEET, HOLD_HEAD);
    sh.getRange(h._row, 9, 1, 4).setValues([['closed', price, today(), Math.round(pnl * 10000) / 10000]]);
  } finally { lock.releaseLock(); }
  var days = Math.round((new Date(today()) - new Date(h.date)) / 86400000);
  return (pnl >= 0 ? '💰 ' : '📉 ') + '已平倉：' + code + ' ' + h.name + '\n' +
    (h.side === 'long' ? '做多 ' : '放空 ') + fmt(h.price) + ' → ' + fmt(price) + '（' + pct(pnl) + '，' + days + ' 天）\n' +
    (pnl < -STOP_PCT - 0.005 ? '⚠️ 虧損超過 5%，下次記得停損要守住。' : '輸入「績效」可以看累計勝率。');
}

function deleteHolding(code) {
  var list = openHoldings(false).filter(function (h) { return h.code === code; });
  if (!list.length) return '沒有 ' + code + ' 的持股紀錄。';
  sheet(HOLD_SHEET, HOLD_HEAD).deleteRow(list[0]._row);
  return '已刪除 ' + code + ' 的紀錄（不算績效）。';
}

function saveAdvice(items) {
  var lock = LockService.getScriptLock(); lock.waitLock(10000);
  try {
    var sh = sheet(ADVICE_SHEET, ['code', 'side', 'json', 'updated']);
    if (sh.getLastRow() > 1) sh.getRange(2, 1, sh.getLastRow() - 1, 4).clearContent();
    var vals = items.map(function (x) { return ["'" + x.code, x.side, JSON.stringify(x), new Date()]; });
    if (vals.length) sh.getRange(2, 1, vals.length, 4).setValues(vals);
  } finally { lock.releaseLock(); }
}

function liveAlert(h, price) {  // 跟 GitHub 13:00 檢查同一套判斷
  var stop = stopOf(h), g = gainOf(h, price);
  var ma5 = h.advice && h.advice.sum4 ? (Number(h.advice.sum4) + price) / 5 : null;
  if (h.side === 'long') {
    if (price <= stop) return '🔴 跌破停損 ' + fmt(stop) + '，收盤前考慮出場';
    if (g >= 0.10 && ma5 && price < ma5 && h.mode === '短') return '🔴 漲幅超過 10%，目前跌破 5 日線 ' + fmt(ma5) + '，收盤確認就停利';
    if (price <= stop * 1.015) return '🟡 接近停損 ' + fmt(stop);
  } else {
    if (price >= stop) return '🔴 突破停損 ' + fmt(stop) + '，收盤前考慮回補';
    if (g >= 0.05 && ma5 && price > ma5) return '🔴 獲利超過 5%，目前站上 5 日線 ' + fmt(ma5) + '，收盤確認就回補';
    if (price >= stop * 0.985) return '🟡 接近停損 ' + fmt(stop);
  }
  return null;
}

function holdingsText() {
  var list = openHoldings(true);
  if (!list.length) return '目前沒有持股紀錄。\n記錄方式：買 2317 210（放空：空 2317 210）';
  var q = liveQuotes(list);
  var anyLive = list.some(function (h) { return q[h.code] && q[h.code].date === Utilities.formatDate(new Date(), 'Asia/Taipei', 'yyyyMMdd'); });
  var lines = ['📋 持股 ' + list.length + ' 檔（' + (anyLive ? Utilities.formatDate(new Date(), 'Asia/Taipei', 'MM/dd HH:mm') + ' 即時' : '最近收盤') + '）'];
  list.forEach(function (h) {
    var price = q[h.code] ? q[h.code].price : (h.advice ? Number(h.advice.close) : h.price);
    var g = gainOf(h, price), stop = stopOf(h);
    var dist = h.side === 'long' ? price / stop - 1 : stop / price - 1;
    lines.push('', h.code + ' ' + h.name + '｜' + (h.side === 'long' ? '做多' : '放空') + '｜' + MODE_NAME[h.mode]);
    lines.push('  成本 ' + fmt(h.price) + ' → 現價 ' + fmt(price) + '（' + pct(g) + '）');
    lines.push('  停損 ' + fmt(stop) + '（還差 ' + (dist * 100).toFixed(1) + '%）');
    var alert = liveAlert(h, price);
    if (alert) lines.push('  ' + alert);
    if (h.advice && h.advice.text) {
      lines.push('  ' + (ICON[h.advice.level] || '') + '昨晚判斷：' + h.advice.level + '｜' + h.advice.text);
    } else {
      lines.push('  （今晚掃描後會有守則建議）');
    }
  });
  return lines.join('\n');
}

function performanceText() {
  var done = rows(HOLD_SHEET, HOLD_HEAD).filter(function (r) { return r.status === 'closed' && r.pnl !== ''; });
  if (!done.length) return '還沒有平倉紀錄。平倉方式：賣 2317 230';
  var pn = done.map(function (r) { return Number(r.pnl); });
  var win = pn.filter(function (x) { return x > 0; }), loss = pn.filter(function (x) { return x <= 0; });
  var avg = function (a) { return a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : 0; };
  var wr = win.length / pn.length, aw = avg(win), al = -avg(loss);
  var exp = wr * aw - (1 - wr) * al;
  var bigLoss = loss.filter(function (x) { return x < -STOP_PCT - 0.005; }).length;
  return [
    '📊 績效（已平倉 ' + pn.length + ' 筆）',
    '勝率：' + (wr * 100).toFixed(0) + '%（' + win.length + ' 勝 ' + loss.length + ' 敗）',
    '平均獲利：' + (win.length ? pct(aw) : '—') + '　平均虧損：' + (loss.length ? pct(-al) : '—'),
    '每筆期望值：' + pct(exp),
    '總報酬（單筆相加）：' + pct(pn.reduce(function (s, x) { return s + x; }, 0)),
    '虧損超過 5% 的次數：' + bigLoss + (bigLoss ? '（停損要守住）' : ' ✅'),
    '—',
    '朱老師的基本方程式：勝率 50%、停利 7%、停損 5% → 每筆期望值 +1.0%',
    exp >= 0.01 ? '👍 已經達到基本方程式的水準' : '還沒達到基本方程式，檢討進場位置和停損紀律',
    '（建議每 3 個月檢討一次：有沒有照計畫進出）'
  ].join('\n');
}
