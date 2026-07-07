# Redfish Manager

透過 Redfish API 管理伺服器 BMC 的網頁介面(AMI MegaRAC / MCT 平台實測,架構支援多廠牌)。

## 功能

- **自動探測(Discovery)**:新增設備時自動走訪 Redfish 樹,依設備實際能力產生 UI(電源操作按鈕、感測器區塊都是動態的)
- **監控**:系統資訊、健康狀態、溫度/風扇/PSU 感測器,背景每 30 秒輪詢,歷史存 SQLite
- **電源控制**:依設備支援的 ResetType 動態產生(On / ForceOff / GracefulShutdown / PowerCycle / NMI…)
- **BIOS 韌體更新**:上傳映像 → 自動追蹤刷寫進度;強制要求主機關機(Off)狀態才可更新
- **BIOS 設定備份/還原**:備份 564+ 項 BIOS Attributes 成 JSON;還原前可預覽差異,只推送有變動的設定
- **操作紀錄**:備份/更新/還原的過程與結果(成功/失敗/進行中)都留痕在頁面頂部狀態列

## 環境需求

- Python 3.10 以上
- 可連到 BMC 管理網路(HTTPS, self-signed 憑證亦可)
- Windows / Linux 皆可執行

## 安裝步驟

```bash
# 1. 取得程式(git clone 或直接複製整個 redfish_manager 資料夾)
cd redfish_manager

# 2. (建議)建立虛擬環境
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 3. 安裝相依套件
pip install -r requirements.txt
```

## 設定

在專案根目錄建立 `redfish_manager.env`(第一台設備,啟動時自動加入):

```
BMC_HOST=192.168.0.11
BMC_USER=root
BMC_PASS=yourpassword
POLL_INTERVAL=30
```

> 之後的設備直接在網頁上方的「新增設備」表單加入即可,不需改設定檔。
> 此檔含帳密,已列入 `.gitignore`,請勿提交到版控。

## 啟動

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

瀏覽器開 `http://<這台機器的IP>:8000` 即可使用。
(只自己用的話 `--host 127.0.0.1` 更安全;`0.0.0.0` 代表區網內其他人也連得到)

## BIOS 更新流程

1. 先把主機關機(電源控制 →「正常關機」;Ubuntu 桌面若攔截 ACPI 電源鍵,
   需在 OS 內關機或設定 `HandlePowerKey=poweroff`)
2. 設備卡片 →「BIOS 更新…」→ 選擇映像檔
3. 建議勾選「更新前備份 BIOS 設定」(存到 `bios_backups/`)
4. 開始更新後對話框會顯示刷寫進度;刷寫期間 BMC 短暫斷線屬正常現象
5. 完成後開機,若要找回舊設定:對話框下方「BIOS 設定還原」→ 選備份檔 →
   「比對差異」預覽 →「還原設定」→ 主機重開機生效

## 獨立命令列工具(不需啟動網頁服務)

```bash
# BIOS 更新(--preserve 先備份設定;會先問確認,--yes 跳過)
python bios_update.py <BMC_IP> <USER> <PASSWORD> <image.bin> --preserve

# BIOS 設定還原(預設讀 bios_setting_prev.json;推送前列出差異並確認)
python bios_restore.py <BMC_IP> <USER> <PASSWORD> [backup.json]
```

## 專案結構

```
redfish_manager/
├── app/
│   ├── main.py            # FastAPI 入口 + API + 背景輪詢
│   ├── redfish_client.py  # Redfish client(discovery / snapshot / BIOS 操作)
│   └── db.py              # SQLite(設備、快照歷史、操作紀錄)
├── web/index.html         # 前端(單頁,無 build 步驟)
├── bios_update.py         # 獨立 BIOS 更新工具
├── bios_restore.py        # 獨立 BIOS 還原工具
├── bios_backups/          # BIOS 設定備份(自動建立)
├── redfish_manager.env    # BMC 連線設定(需自行建立,不進版控)
└── redfish_manager.db     # SQLite 資料庫(自動建立)
```

## 注意事項

- BMC 憑證為 self-signed,程式已跳過驗證(`verify=False`)
- 網頁本身**沒有登入機制**,請只在受信任的管理網段開放
- BIOS 實際刷寫請務必使用該機型對應的映像檔;刷寫中不可斷 BMC 電源
- 還原設定時會自動略過三個已知跨版本格式會變的項目
  (`CbsCmnCxlComponentErrorReporting`、`CbsCmnFchSystemPwrFailShadow`、`IPMI610`)
