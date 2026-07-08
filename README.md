# Redfish Manager

透過 Redfish API 管理伺服器 BMC 的網頁介面(AMI MegaRAC / MCT 平台實測,架構支援多廠牌)。

## 功能

- **自動探測(Discovery)**:新增設備時自動走訪 Redfish 樹,依設備實際能力產生 UI(電源操作按鈕、感測器區塊都是動態的)
- **監控**:系統資訊、健康狀態、溫度/風扇/PSU 感測器,背景每 30 秒輪詢,歷史存 SQLite
- **電源控制**:依設備支援的 ResetType 動態產生(On / ForceOff / GracefulShutdown / PowerCycle / NMI…)
- **BIOS 韌體更新**:上傳映像 → 自動追蹤刷寫進度;強制要求主機關機(Off)狀態才可更新
- **BIOS 設定備份/還原**:備份數百項 BIOS Attributes 成 JSON;還原前可預覽差異,只推送有變動的設定
- **SEL 事件日誌**:讀取 IPMI SEL / BMC Event / Audit 等日誌服務,新到舊排列,可清除
- **操作紀錄**:備份/更新/還原的過程與結果(成功/失敗/進行中)都留痕在頁面頂部狀態列
- **中英雙語**:右上角開關即時切換,選擇記憶在瀏覽器

## 環境需求

- Python 3.10 以上
- 可連到 BMC 管理網路(HTTPS, self-signed 憑證亦可)
- Windows / Linux 皆可執行

## 取得程式(從私有 GitHub repo clone)

本專案放在**私有** repo,需要先請擁有者(marlborok)到
GitHub → repo → Settings → Collaborators 邀請你,接受邀請後才能 clone。

```bash
git clone https://github.com/marlborok/redfish-manager.git
cd redfish-manager
```

> 第一次 clone/push 時,Windows 的 Git 憑證管理員可能會跳出瀏覽器要你登入 GitHub 授權,完成即可。
> 沒有 git 也可以請擁有者用 GitHub 的「Download ZIP」下載後解壓。

## 安裝步驟

```bash
# 1. (建議)建立虛擬環境
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 2. 安裝相依套件
pip install -r requirements.txt
```

## 設定

複製範例檔並填入你的 BMC 連線資訊(這是啟動時自動加入的第一台設備):

```bash
# Windows:
copy redfish_manager.env.example redfish_manager.env
# Linux/macOS:
cp redfish_manager.env.example redfish_manager.env
```

編輯 `redfish_manager.env`:

```
BMC_HOST=192.168.0.11
BMC_USER=root
BMC_PASS=yourpassword
POLL_INTERVAL=30
```

> 之後的設備直接在網頁上方的「新增設備」表單加入即可,不需改設定檔。
> `redfish_manager.env` 含帳密,已列入 `.gitignore`,不會被提交;範例檔 `redfish_manager.env.example` 才會進版控。

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

### ipmitool CLI(內部用,支援所有 ipmitool 指令)

透過 LAN 對 BMC 執行任意 ipmitool 指令;帳密預設讀 `redfish_manager.env`,
可用 `-H/-U/-P` 覆寫。**需先安裝 ipmitool 二進位**(Linux:`apt/yum install ipmitool`)。

```bash
python ipmi_cli.py sel list
python ipmi_cli.py chassis status
python ipmi_cli.py -H 192.168.0.47 -U root -P secret mc info
python ipmi_cli.py -I lan raw 0x32 0xaa 0x00      # 預設 interface 為 lanplus
```

> 密碼透過環境變數 `IPMI_PASSWORD`(ipmitool `-E`)傳入,不會出現在行程清單;
> 指令以陣列執行(非 shell),不受命令注入影響。核心邏輯在 `app/ipmi.py`,
> 未來要接進網頁只需加一個帶認證+白名單的 endpoint,核心不需改動。

## 專案結構

```
redfish_manager/
├── app/
│   ├── main.py            # FastAPI 入口 + API + 背景輪詢
│   ├── redfish_client.py  # Redfish client(discovery / snapshot / BIOS 操作)
│   ├── ipmi.py            # ipmitool 包裝核心(CLI 與未來 web 共用)
│   └── db.py              # SQLite(設備、快照歷史、操作紀錄)
├── web/index.html         # 前端(單頁,無 build 步驟)
├── bios_update.py         # 獨立 BIOS 更新工具
├── bios_restore.py        # 獨立 BIOS 還原工具
├── ipmi_cli.py            # 獨立 ipmitool CLI(需安裝 ipmitool)
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
