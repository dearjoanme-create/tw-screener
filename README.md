# 台股型態選股器

每個交易日晚上自動掃描全部上市＋上櫃股票，找出三種型態，算好「明天的觸發價」用 LINE 推給你；隔天 11:00、13:00 自動檢查有沒有突破，再推一次。全部跑在 GitHub 上，免費、不用開電腦。

| 時間（台灣） | 做什麼 |
|---|---|
| 週一～五 約 20:20 | 抓當天收盤 → 選股 → LINE 推播候選清單 |
| 隔天 約 11:00 | 檢查候選股有沒有站上觸發價 → LINE 推播 |
| 隔天 約 13:00 | 再檢查一次 → LINE 推播 |

> GitHub 的排程常會晚 5～15 分鐘，屬正常現象。假日、颱風假會自動跳過。

---

## 三種型態的規則

**1. 多頭回檔再上漲**
- 收盤 > 20MA > 60MA，60MA 比 5 天前高
- 最近兩個波段：頭頭高、底底高
- 前一天收盤回到 5MA 附近（5MA 上方 1% 到下方 3% 之間），且量比 5 日均量小
- **觸發價** = 「站回 5MA 需要的價格」和「前一天高點」取高者

**2. 突破下降切線**
- 近 60 天最高的波段高點，連到之後一個低 8% 以上的高點，畫出切線（中間沒被突破過）
- 前一天收盤在切線下方 3% 以內
- **觸發價** = 切線延伸到明天的位置；量要放大到 5 日均量 1.2 倍

**3. 突破盤整區間**
- 最近 15～40 天的高低差在 10% 以內（取最長的那段）
- 前一天收盤在區間高點下方 3% 以內，且盤整期間量縮
- **觸發價** = 區間高點；量要放大到 5 日均量 1.5 倍

**共同過濾**：只看一般股票（排除 ETF、權證）、股價 ≥ 10 元、5 日均量 ≥ 1,000 張、觸發價離收盤 3% 以內。

**相對強度**：近 60 天漲幅在全市場的排名（100 = 最強）。回檔型、盤整型要前 30%（≥70），切線型要前 50%（≥50）。清單依強度排序，LINE 每類只推前 5 名。

**資料來源**：上市用證交所；上櫃當天用櫃買中心，歷史資料用 Yahoo 補（櫃買中心查不到過去日期）。

盤中檢查時，量能是用「目前成交量 ÷ 已開盤時間 × 全天」推估的，早盤量通常比較大，11:00 的推估會偏樂觀，參考就好。

所有數字都在 `config.py`，想調整直接改那個檔案。

---

## 第一次設定（約 30 分鐘）

### 步驟 1：拿到 LINE 的兩個金鑰

1. 到 [LINE Official Account Manager](https://manager.line.biz/) 登入，選你的官方帳號
2. 右上角「設定」→ 左邊「Messaging API」→ 按「啟用 Messaging API」，選一個提供者（Provider，沒有就新建一個，名字隨便取）
3. 到 [LINE Developers](https://developers.line.biz/console/) 登入，點剛剛的提供者 → 點你的官方帳號那個 channel
4. 在 **Basic settings** 分頁拉到最下面，找到 **Your user ID**（U 開頭的一長串）→ 複製下來，這是 `LINE_USER_ID`
5. 在 **Messaging API** 分頁拉到最下面，**Channel access token (long-lived)** 按「Issue」→ 複製下來，這是 `LINE_CHANNEL_ACCESS_TOKEN`
6. 同一頁有 QR code，**用手機掃描把你的官方帳號加為好友**（沒加好友收不到推播）

> 這兩個值等於官方帳號的鑰匙，不要貼在任何公開的地方。

### 步驟 2：開 GitHub 帳號、建立專案

1. 到 [github.com](https://github.com) 註冊帳號（免費）
2. 右上角「＋」→「New repository」
   - Repository name：`tw-screener`
   - 選 **Public**（免費帳號要 Public 才能開網頁；金鑰另外存放，不會公開）
   - 按「Create repository」
3. 在新頁面點「uploading an existing file」
4. 把解壓縮後 `tw-screener` 資料夾**裡面的所有檔案和資料夾**一起拖進去（包含 `.github` 資料夾，這個很重要，排程設定在裡面）
5. 拉到下方按「Commit changes」

> 上傳完確認一下，頁面上應該看得到 `.github`、`docs`、`tests` 三個資料夾和 `scan.py` 等檔案。如果沒有 `.github`，就另外點「Add file → Upload files」單獨把它拖進去。

### 步驟 3：把 LINE 金鑰存到 GitHub

1. 在專案頁面點上方「Settings」
2. 左邊「Secrets and variables」→「Actions」→ 綠色按鈕「New repository secret」
3. Name 填 `LINE_CHANNEL_ACCESS_TOKEN`，Secret 貼上步驟 1 的 token → Add secret
4. 再新增一個：Name 填 `LINE_USER_ID`，Secret 貼上 U 開頭的那串 → Add secret

### 步驟 4：開啟網頁

1. 還是在「Settings」，左邊點「Pages」
2. Source 選「Deploy from a branch」，Branch 選 `main`、資料夾選 `/docs` → Save
3. 等 1～2 分鐘，上方會出現網址，長得像 `https://你的帳號.github.io/tw-screener/`，用手機加到主畫面

### 步驟 5：第一次手動執行

1. 點上方「Actions」分頁（如果有提示要啟用，按綠色按鈕啟用）
2. 左邊點「晚上選股掃描」→ 右邊「Run workflow」→ 再按一次綠色「Run workflow」
3. **第一次要回補半年歷史資料，大約 20～40 分鐘**，之後每天只要 2～3 分鐘
4. 跑完（出現綠色勾勾）LINE 就會收到第一份清單，網頁也會有資料

之後就全自動了，不用再做任何事。

---

## 常見問題

**有紅色叉叉（執行失敗）怎麼辦？**
點進去那次執行 → 點有紅叉的步驟，把錯誤訊息截圖給我，我幫你看。

**想排除某些股票（例如全額交割股、不想碰的股票）？**
在 GitHub 上點 `exclude.txt` → 右上角鉛筆圖示編輯 → 一行寫一個代號 → Commit changes。

**想改參數？**
同樣方式編輯 `config.py`，改完下一次執行就生效。想馬上看結果，到 Actions 手動 Run workflow。

**GitHub 寄信說排程被停用？**
長時間沒有人動專案時 GitHub 可能會暫停排程，到 Actions 頁面按「Enable workflow」就恢復。

---

## 已知限制

- 使用的是**未還原權息**的價格，除權息當天的跳空會讓型態判斷失準，那幾天的訊號要自己多看一眼
- 證交所、櫃買中心的網站格式偶爾會改，如果某天突然抓不到資料，把錯誤訊息給我就能修
- 這是技術面篩選工具，只負責找出值得看的股票，**不構成投資建議**，下單前請自己判斷

---

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `config.py` | 所有參數 |
| `data.py` | 抓證交所／櫃買中心每日行情 |
| `patterns.py` | 三種型態的判斷邏輯 |
| `scan.py` | 晚上選股＋推播 |
| `check.py` | 盤中檢查＋推播 |
| `line_push.py` | LINE 推播 |
| `docs/index.html` | 手機網頁 |
| `exclude.txt` | 排除清單 |
| `.github/workflows/` | 自動排程設定 |
| `tests/` | 測試（用模擬走勢驗證三種型態） |
