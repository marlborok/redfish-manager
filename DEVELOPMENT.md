# 開發筆記(設計決策 / 待辦 / 踩坑 / 未來規劃)

本文件記錄 Redfish Manager 的開發脈絡,供協作者理解「為什麼這樣做」以及目前狀態。
使用/安裝說明請見 [README.md](README.md)。

---

## 1. 設計決策與原因

### 架構整體:Discovery 驅動 + 正規化層
- **捨棄「純 Schema 驅動 UI」**:完全依 Redfish schema 動態產生介面看似優雅,但實務上做出來像
  Swagger UI——什麼都能看、沒有工作流程,且各廠牌 schema 差異大、邊界情況多。
- **採用「Discovery 驅動 + 後端正規化」**:啟動時走訪 Redfish 樹(`discover()`),把設備能力
  存成 profile;背景 collector 定期輪詢並把各廠牌資料**正規化成統一 snapshot 模型**。
  前端只面對我們的 API,不直接碰 Redfish → 廠牌差異隔離在一處,換牌/加牌只改 collector。
- **控制項由能力產生**:電源按鈕不是寫死,而是依該設備 `ResetActionInfo` 的
  `AllowableValues` 動態生成;讀不到的感測器自動不顯示(拔掉風扇就自動消失,裝回自動出現)。

### 技術選型
- **後端 FastAPI + async httpx + SQLite + 背景輪詢**:純 Python、無外部二進位依賴(IPMI 之前)。
- **前端單一 HTML、無 build 步驟**:降低部署與維護成本。
- **SQLite 三張表**:`devices`(含 profile)、`snapshots`(歷史,每設備留約一天)、
  `activities`(操作紀錄)。

### i18n:後端存「訊息代碼 + 參數」而非成品字串
- 前端 i18n 用集中字典 + `t(key, ...args)`;靜態元素用 `data-i18n` 屬性。
- **操作紀錄的訊息也要能跟著語言切換**:比較過兩案——
  - (B) 前端每次帶語言、後端即時翻譯 → 歷史紀錄仍是舊語言。
  - **(A,採用)** 後端只存 `msg_code + msg_params`,前端渲染 → **連歷史紀錄切語言也會變**。
- 交付英語客戶前建議清空 `activities`(舊資料是改造前的成品中文字串,會 fallback 顯示中文)。

### 深色主題
- 全部顏色抽成 **CSS 變數 token**(`:root` / `:root[data-theme="dark"]`)。
- 預設跟隨 OS `prefers-color-scheme`;`<head>` 內早期 inline script 在首次繪製前套用主題,**避免閃爍**。

### 按鈕與版面
- 統一 `.btn` 系統(`btn-primary` / `btn-ghost` / `btn-danger`),取代原本三套各異的按鈕樣式。
- **電源控制**與**管理與檢視**(BIOS 更新 / 事件日誌 / IPMI 主控台)分成兩組,避免把唯讀操作
  跟破壞性電源操作混在一起、外觀相同而誤觸。

### 自動刷新的取捨
- 現行架構是每 15 秒整段重繪 `#app`(會摺疊展開的區塊、重置捲動、清掉操作回饋)。
- **未改成差異更新**(成本較高),改採**互動時暫停重繪**:滑鼠停在內容區、有展開的 `<details>`、
  或有操作進行中 → 背景仍抓資料但跳過重繪;手動操作則強制重繪。務實、風險低。

### 電源按鈕狀態感知
- 依即時 `PowerState` 反灰不適用的動作(關機時反灰強制斷電/正常關機/重啟/電源循環/NMI;
  開機時反灰開機/ForceOn),並附滑鼠提示原因。前後端雙重把關(前端擋、後端以 BMC 即時狀態為準)。

### IPMI:共用核心 + 雙後端 + 子行程隔離
- **核心邏輯放可 import 的 `app/ipmi.py`**,CLI(`ipmi_cli.py`)是薄包裝 → 未來接 web 核心零改動。
- **雙後端**:`ipmitool` 二進位(最忠實、完整 CLI)vs `pyghmi` 純 Python(免裝、跨平台);
  `backend="auto"` 有 ipmitool 用它、否則 fallback pyghmi。
- **web 端每指令用獨立子行程執行**(`run_ipmi_isolated`,見踩坑 §3),而非長駐 in-process pyghmi。

### BIOS 更新/還原:移植廠商 shell 腳本並改良
- 上傳走 AMI 專有 `POST /redfish/v1/UpdateService/upload`(multipart 三欄位)。
- **還原改用 Python dict 語意比對**取代 shell 的 `diff + sed`(較脆弱):只推送真正有差異的鍵,
  並沿用廠商的「跨版本易變屬性 ignore list」;推送前列出差異供確認(shell 版看不到就直接推)。
- 強制「主機關機(Off)才可刷 BIOS」——前端擋 + 後端以 BMC 即時 PowerState 驗證(回 409)。

---

## 2. 待辦 / 已知問題

### 高優先
- [ ] **web 完全沒有認證**。IPMI 主控台能執行任意指令,無認證 = 區網任何人可對伺服器下指令。
      上線前必須補(見 §4)。
- [ ] **Redfish/裝置密碼以明文存於 SQLite**(`devices.password`)。應加密或改用密鑰管理。

### 功能缺口
- [ ] **刪除設備**功能未做(UI 無按鈕、後端無 endpoint)。需連帶清 snapshots/activities;
      且若刪的是 `.env` 那台,啟動時 `ensure_device` 會自動加回(需設計:提示改 .env,或記錄已刪 host)。
- [ ] profile 原始 JSON dump 仍直接顯示給終端使用者(應改成結構化硬體清冊頁,見 §4)。
- [ ] IPMI 主控台「全部記錄」會讓操作紀錄被 IPMI 指令洗版,UI 無 kind 篩選。
- [ ] pyghmi 後端非 ipmitool CLI 完整替身:廠商專屬子命令(如 `lan print`、`delloem`)未對應
      (底層可用 `raw` 湊)。

### 體驗/細節
- [ ] 溫度紅字門檻 `>= UpperThresholdCritical - 10` 可能誤報(曾見 40°C 標紅),需檢查門檻。
- [ ] 無 favicon、無載入 skeleton(只有純文字「載入中…」)。
- [ ] 操作紀錄用絕對時間,可考慮相對時間(「3 分鐘前」)。
- [ ] 無障礙:語言切換缺 `aria-pressed`、圖示按鈕缺 `aria-label`。
- [ ] 自動刷新為整段重繪,可改差異更新以徹底解決狀態保留問題。

---

## 3. 踩過的坑

### AMI MegaRAC / Redfish 相容性
- **`$skip=0` 回 HTTP 400**:AMI 拒絕 `$skip=0`,只有真的要跳過時才帶此參數。
- **SEL `$top` 上限 50**:一次最多取最新 50 筆(BMC 端限制),UI 標註總筆數。
- **OEM `InventoryData` 等回 403**:部分 `Oem/Ami/*` 需特殊權限。

### pyghmi(這次最大的坑)
- **session 非執行緒安全 + 全域 per-BMC session 快取**:`run_in_threadpool` 併發呼叫會互鎖 →
  指令卡死、504。
- **手動 `logout()` 反而壞事**:pyghmi 快取並重用 session,logout 掉共享 session 後,
  下一指令重用到已登出 session → `TypeError: '<' not supported between float and NoneType`。
- **in-process 重用會耗盡 BMC session 槽**:長駐服務不釋放 session,累積到 BMC 上限後新指令掛起。
- **解法**:web 端**每指令開獨立子行程**(`run_ipmi_isolated` 透過 `ipmi_cli.py`),行程結束即
  釋放 session——等同一直很穩的 CLI 模式;另加 `asyncio.wait_for` 超時防線。
- **測試副作用**:hung session 會佔住 BMC session 槽,需約 60~75 秒逾時才釋放,期間連 CLI 也會卡。

### 環境 / 平台
- **Windows cp1252 印中文報 `UnicodeEncodeError`**:腳本輸出用 `PYTHONIOENCODING=utf-8`。
- **httpx 不會連帶安裝 urllib3**:`main.py` 直接 `import urllib3`(disable_warnings),
  新環境會缺 → 已明確列入 `requirements.txt`。
- **`httpx.ConnectTimeout` 的 `str(e)` 為空字串**:導致連線失敗時狀態列訊息空白 →
  改成 `str(e) or type(e).__name__`。
- **孤兒 `running` 操作紀錄**:行程崩潰/重啟後殘留 running → 啟動時 `fail_orphan_activities` 標為失敗。
- **Windows git 的 LF→CRLF 警告**:提交時常見,無害。

### 韌體 / 硬體行為
- **Ubuntu 桌面攔截 ACPI 電源鍵**:GNOME 有登入 session 時 GracefulShutdown 不會真的關機
  (跳確認框 60 秒後取消)→ 需在 OS 內關機或設 `HandlePowerKey=poweroff`。
- **BIOS 更新確實會刷入並重啟**:曾觀察到版本 V3.04 → V0.05、BMC 一併短暫離線
  (輪詢顯示 ConnectTimeout),屬正常現象。

---

## 4. 未來規劃

### 安全(最優先)
- **web 認證層**:HTTP Basic / 共用密碼 / session 登入,至少一種。這是 IPMI 主控台與整體對外
  開放的**前置條件**。
- **IPMI web endpoint 的白名單模式**:內部用維持全指令;若要給非內部環境,加「唯讀子命令白名單」
  (sel/sdr/sensor/fru/mc info/power status),寫入類(power off/raw/bmc reset)另嚴格控管。

### 功能
- **硬體清冊頁**:DIMM(24 槽)/儲存/網卡 結構化呈現,取代 profile JSON dump。
- **EventService 訂閱(SSE/webhook)**:BMC 主動推事件,取代/補強輪詢。
- **溫度/功耗歷史圖表**:資料已在 `snapshots` 累積,可直接畫趨勢。
- **刪除設備**功能(含 .env 重加的處理)。
- **相對時間、favicon、載入 skeleton、無障礙**補強。

### 架構
- 前端改**差異更新**取代整段重繪,徹底解決刷新時的狀態保留。
- IPMI 若要高頻/多台並發,評估單一常駐 worker thread 或連線管理策略(目前子行程隔離已足夠內部用)。
