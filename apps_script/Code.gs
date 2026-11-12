/**
 * 台股趨勢選股 LINE 小幫手（Google Apps Script）
 *
 * LINE 指令：
 *   2317            查個股分析（前一天收盤的結果）
 *   買 2317 210     記錄做多（可加 短／中／長／飆，預設短線），例：買 2317 210 中
 *   買 2317 210 停損 200   自訂停損（沒寫就用選股建議的停損，沒有建議就用固定 5%）
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
                 'exit_price', 'exit_date', 'pnl', 'created', 'stop'];
var MODE_NAME = { '短': '短線', '中': '中線', '長': '長線', '飆': '飆股' };
var TAKE_PROFIT = 0.10, STOP_MAX = 0.10;
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
  if (p.action === 'signals') {  // 還沒補完事後結果的選股紀錄
    if (!prop('SECRET') || p.secret !== prop('SECRET')) return json({ ok: false, error: 'secret 不對' });
    return json(pendingSignals());
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

  if (body.action === 'log' || body.action === 'outcomes') {  // GitHub 送來的選股、盤中紀錄
    if (!prop('SECRET') || body.secret !== prop('SECRET')) return json({ ok: false, error: 'secret 不對' });
    var lock = LockService.getScriptLock(); lock.waitLock(30000);
    try {
      if (body.action === 'log') logRows(body.sheet, body.head, body.text_cols || [], body.match || {}, body.rows || []);
      else saveOutcomes(body.sheet, body.head, body.items || []);
    } finally { lock.releaseLock(); }
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
  if ((m = s.match(/^(買|多|做多|空|放空)\s*(\d{4})\s+(\d+(?:\.\d+)?)\s*(短|中|長|飆)?(?:\s*停損\s*(\d+(?:\.\d+)?))?$/)))
    return addHolding(m[2], /空/.test(m[1]) ? 'short' : 'long', Number(m[3]), m[4] || '短', m[5] ? Number(m[5]) : null);
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
    '買 2317 210 中　用中線規則（短／中／長／飆）',
    '買 2317 210 停損 200　自訂停損',
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
          (x.need_vol ? (x.vol_ref ? '、量 ≥ ' + fmt(Math.round(x.vol_ref * x.need_vol)) + ' 張（昨量 ' + x.need_vol + ' 倍）'
            : '、量 ≥ 平常的 ' + x.need_vol + ' 倍') : '') +
          '，停損 ' + fmt(x.stop) + (x.stop_method ? '（' + x.stop_method + '）' : '') + (x.term ? '｜' + x.term : ''));
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
  } else if (sh.getLastColumn() < head.length) {  // 新版多了欄位：表頭往後補齊
    sh.getRange(1, 1, 1, head.length).setValues([head]);
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
      date: dateStr(r.date), mode: r.mode || '短', stop: Number(r.stop) || null,
      advice: adv[code + r.side] || null, _row: r._row
    };
  });
}

function gainOf(h, price) {
  return h.side === 'long' ? price / h.price - 1 : (h.price - price) / h.price;
}

function stopOf(h) {
  if (h.stop) return Number(h.stop);
  if (h.advice && h.advice.stop) return Number(h.advice.stop);
  return h.side === 'long' ? h.price * (1 - STOP_PCT) : h.price * (1 + STOP_PCT);
}

function today() { return Utilities.formatDate(new Date(), 'Asia/Taipei', 'yyyy-MM-dd'); }

function okStop(side, price, stop) {  // 停損要在進場價的正確一側、而且不超過 10%
  if (!stop) return false;
  var d = side === 'long' ? (price - stop) / price : (stop - price) / price;
  return d > 0 && d <= STOP_MAX;
}

function addHolding(code, side, price, mode, myStop) {
  var r = stockRecord(code);
  if (!r) return '找不到 ' + code + '，請確認代號。';
  var dup = openHoldings(false).filter(function (h) { return h.code === code && h.side === side; });
  if (dup.length) return '已經有 ' + code + ' 的' + (side === 'long' ? '做多' : '放空') + '紀錄了。要改的話先輸入「刪除 ' + code + '」再重新記錄。';
  if (Math.abs(price / r.close - 1) > 0.15) return '價格 ' + price + ' 跟最近收盤 ' + r.close + ' 差太多，請確認有沒有打錯。';
  // 停損：自己寫的 > 選股建議（依型態） > 固定 5%
  var stop = null, how = '';
  if (myStop) {
    if (!okStop(side, price, myStop)) return '停損 ' + myStop + ' 不對：做多要低於買進價、放空要高於，而且不超過 10%。';
    stop = myStop; how = '自訂';
  } else {
    var sug = (r.setups || []).filter(function (x) { return x.side === side && !(x.why && x.why.length); })[0];
    if (sug && okStop(side, price, Number(sug.stop))) { stop = Number(sug.stop); how = sug.stop_method || '依型態'; }
  }
  if (!stop) { stop = side === 'long' ? price * (1 - STOP_PCT) : price * (1 + STOP_PCT); how = '固定 5%'; }
  stop = Math.round(stop * 100) / 100;
  var lock = LockService.getScriptLock(); lock.waitLock(10000);
  try {
    sheet(HOLD_SHEET, HOLD_HEAD).appendRow([Utilities.getUuid().slice(0, 8), "'" + code, r.name, r.market, side, price,
      today(), mode, 'open', '', '', '', new Date(), stop]);
  } finally { lock.releaseLock(); }
  var up = side === 'long', line = mode === '中' ? '10 日線' : '5 日線';
  var rules;
  if (mode === '長') {
    rules = ['沒' + (up ? '跌破' : '站上') + '月線就續抱', '獲利超過 20%、乖離超過 20%，' + (up ? '跌破' : '站上') + ' 10 日線停利',
             '趨勢反轉確認就先停利出場'];
  } else if (mode === '飆') {
    rules = ['沒破前 2 日低、沒破 3 日均線、沒出黑K就續抱', '量大增、開高走低出黑K就賣出'];
  } else {
    rules = [(up ? '漲' : '跌') + '幅未達 10%，' + (up ? '跌破 ' : '站上 ') + line + ' 續抱',
             (up ? '漲' : '跌') + '幅超過 10%，收盤' + (up ? '跌破 ' : '站上 ') + line + ' 停利',
             '獲利 20% 以上出現爆量長' + (up ? '黑或長上影線' : '紅或長下影線') + '先出一半，隔天續' + (up ? '跌' : '漲') + '全出',
             '出現' + (up ? '頭頭低' : '底底高') + '（趨勢改變）就出場'];
  }
  return [
    '✅ 已記錄：' + (side === 'long' ? '做多 ' : '放空 ') + code + ' ' + r.name + ' ' + fmt(price) + '（' + MODE_NAME[mode] + '）',
    '停損：' + fmt(stop) + '（' + how + '，收盤' + (side === 'long' ? '跌破' : '突破') + '就出場，設好不改）',
    '操作規則：', '・' + rules.join('\n・'),
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
  var stop = stopOf(h), g = gainOf(h, price), a = h.advice || {}, line = null, name = '';
  if (h.mode === '中' && a.sum9) { line = (Number(a.sum9) + price) / 10; name = '10 日線'; }
  else if (h.mode === '短' && a.sum4) { line = (Number(a.sum4) + price) / 5; name = '5 日線'; }
  if (h.side === 'long') {
    if (price <= stop) return '🔴 跌破停損 ' + fmt(stop) + '，收盤前考慮出場';
    if (g >= TAKE_PROFIT && line && price < line) return '🔴 漲幅超過 10%，目前跌破 ' + name + ' ' + fmt(line) + '，收盤確認就停利';
    if (price <= stop * 1.015) return '🟡 接近停損 ' + fmt(stop);
  } else {
    if (price >= stop) return '🔴 突破停損 ' + fmt(stop) + '，收盤前考慮回補';
    if (g >= TAKE_PROFIT && line && price > line) return '🔴 獲利超過 10%，目前站上 ' + name + ' ' + fmt(line) + '，收盤確認就回補';
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
      lines.push('  （今晚掃描後會有操作建議）');
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
    '參考基準：勝率 50%、停利 7%、停損 5% → 每筆期望值 +1.0%',
    exp >= 0.01 ? '👍 已經達到參考基準' : '還沒達到參考基準，檢討進場位置和停損紀律',
    '（建議每 3 個月檢討一次：有沒有照計畫進出）'
  ].join('\n');
}

// ------------------------------------------------------------------
// 選股紀錄、盤中紀錄（GitHub 每天自動寫入）
// ------------------------------------------------------------------
function cellStr(v, key) {
  if (v instanceof Date) {
    return key === '時間' ? Utilities.formatDate(v, 'Asia/Taipei', 'HH:mm') : dateStr(v);
  }
  return String(v === null || v === undefined ? '' : v).replace(/^'/, '');
}

function logSheet(name, head) {
  var sh = sheet(name, head);
  if (sh.getLastColumn() < head.length) {  // 新版多了欄位：表頭往後補齊
    sh.getRange(1, 1, 1, head.length).setValues([head]);
  }
  if (name === '選股紀錄') { try { setupStats(sh, head); } catch (err) { /* 不影響寫入 */ } }
  return sh;
}

function logRows(name, head, textCols, match, list) {
  var sh = logSheet(name, head);
  var keys = Object.keys(match);
  var last = sh.getLastRow();
  if (last > 1 && keys.length) {  // 同一天（同一時段）重跑就先刪掉舊的，避免重複
    var v = sh.getRange(2, 1, last - 1, head.length).getValues();
    var idx = keys.map(function (k) { return head.indexOf(k); });
    for (var i = v.length - 1; i >= 0; i--) {
      var hit = idx.every(function (j, n) { return j >= 0 && cellStr(v[i][j], keys[n]) === String(match[keys[n]]); });
      if (hit) sh.deleteRow(i + 2);
    }
  }
  if (!list.length) return;
  var vals = list.map(function (r) {
    return head.map(function (k) {
      var x = r[k];
      if (x === null || x === undefined) return '';
      return textCols.indexOf(k) >= 0 ? "'" + x : x;
    });
  });
  sh.getRange(sh.getLastRow() + 1, 1, vals.length, head.length).setValues(vals);
}

function pendingSignals() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('選股紀錄');
  if (!sh || sh.getLastRow() < 2) return { ok: true, total: 0, items: [] };
  var v = sh.getDataRange().getValues(), head = v[0];
  var col = function (k) { return head.indexOf(k); };
  var since = new Date(Date.now() - 120 * 86400000);
  var items = [];
  v.slice(1).forEach(function (r) {
    var res = col('13:00結果') >= 0 ? cellStr(r[col('13:00結果')]) : '';
    var wl = col('勝負') >= 0 ? cellStr(r[col('勝負')]) : '';
    var done = r[col('10日報酬%')] !== '' && res !== '' &&
      (['✅ 可進場', 'KD 擋下', '其他條件擋下'].indexOf(res) < 0 || (wl !== '' && wl !== '持有中'));
    if (done) return;  // 已經補完
    var d = cellStr(r[col('日期')]);
    if (new Date(d) < since) return;  // 太舊的不再更新
    items.push({ id: cellStr(r[col('id')]), date: d, code: cellStr(r[col('代號')]), side: r[col('多空')],
                 trigger: r[col('觸發價')], stop: r[col('停損')], result: res });
  });
  return { ok: true, total: v.length - 1, items: items };
}

function saveOutcomes(name, head, items) {
  var sh = logSheet(name, head);
  if (sh.getLastRow() < 2) return;
  var hd = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  var idCol = hd.indexOf('id');
  var ids = sh.getRange(2, idCol + 1, sh.getLastRow() - 1, 1).getValues().map(function (r) { return cellStr(r[0]); });
  items.forEach(function (it) {
    var row = ids.indexOf(String(it.id));
    if (row < 0) return;
    var keys = Object.keys(it.values);
    var cols = keys.map(function (k) { return hd.indexOf(k); }).filter(function (c) { return c >= 0; });
    if (!cols.length) return;
    var c0 = Math.min.apply(null, cols), c1 = Math.max.apply(null, cols);
    var rng = sh.getRange(row + 2, c0 + 1, 1, c1 - c0 + 1);
    var cur = rng.getValues()[0];
    keys.forEach(function (k) { var c = hd.indexOf(k); if (c >= 0) cur[c - c0] = it.values[k]; });
    rng.setValues([cur]);
  });
}

// 「統計」分頁和顏色：第一次寫入新版欄位時自動建立（之後不會再動，可以自己調整）
var RESULT_COLORS = [['✅ 可進場', '#D9EAD3'], ['KD 擋下', '#FFF2CC'], ['其他條件擋下', '#FCE5CD'],
                     ['拉回', '#E0E0E0'], ['逆大盤', '#F3F3F3']];

function colLetter(n) {  // 1 → A
  var s = '';
  while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}

function setupStats(sh, head) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (ss.getSheetByName('統計')) return;
  var R = colLetter(head.indexOf('13:00結果') + 1), W = colLetter(head.indexOf('勝負') + 1),
      P = colLetter(head.indexOf('報酬%') + 1);
  if (head.indexOf('勝負') < 0) return;
  var rr = "'選股紀錄'!" + R + ':' + R, ww = "'選股紀錄'!" + W + ':' + W, pp = "'選股紀錄'!" + P + ':' + P;

  // 整列依 13:00 結果上色
  var rules = sh.getConditionalFormatRules();
  var all = sh.getRange('A2:' + colLetter(head.length));  // 整欄，之後新增的列也會上色
  RESULT_COLORS.forEach(function (c) {
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenFormulaSatisfied('=$' + R + '2="' + c[0] + '"')
      .setBackground(c[1]).setRanges([all]).build());
  });
  sh.setConditionalFormatRules(rules);

  var st = ss.insertSheet('統計');
  st.getRange(1, 1, 1, 9).setValues([['13:00 結果', '已出場', '賺', '賠', '勝率', '平均報酬%', '平均賺%', '平均賠%', '持有中']]);
  ['✅ 可進場', 'KD 擋下', '其他條件擋下'].forEach(function (k, i) {
    var r = i + 2, a = '$A' + r;
    st.getRange(r, 1, 1, 9).setFormulas([[
      '="' + k + '"',
      '=C' + r + '+D' + r,
      '=COUNTIFS(' + rr + ',' + a + ',' + ww + ',"賺")',
      '=COUNTIFS(' + rr + ',' + a + ',' + ww + ',"賠")',
      '=IFERROR(C' + r + '/B' + r + ',"")',
      '=IFERROR(AVERAGEIFS(' + pp + ',' + rr + ',' + a + ',' + ww + ',"<>持有中",' + ww + ',"<>"),"")',
      '=IFERROR(AVERAGEIFS(' + pp + ',' + rr + ',' + a + ',' + ww + ',"賺"),"")',
      '=IFERROR(AVERAGEIFS(' + pp + ',' + rr + ',' + a + ',' + ww + ',"賠"),"")',
      '=COUNTIFS(' + rr + ',' + a + ',' + ww + ',"持有中")'
    ]]);
  });
  st.getRange('E2:E4').setNumberFormat('0%');
  st.getRange('F2:H4').setNumberFormat('0.00');
  st.getRange(6, 1, 4, 1).setValues([
    ['說明：買進價＝訊號隔天（13:00 檢查那天）的收盤價；賣出價＝照持股的短線規則，出現「出場」那天的收盤價'],
    ['「KD 擋下」「其他條件擋下」是假設當天照樣買進，用來比較這條規則值不值得留'],
    ['做空的報酬已經換算成跌越多賺越多；還沒出場的算「持有中」，不算進勝率'],
    ['這個分頁只會自動建立一次，之後可以自己改']
  ]);
  st.setFrozenRows(1);
}
