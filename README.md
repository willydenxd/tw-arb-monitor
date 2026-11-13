# tw-arb-monitor：台股期貨／選擇權／ETF 即時套利監控

這是用永豐 Shioaji 的即時行情做的監控程式，盤中掃三種價格錯位，並推播到 Telegram：

1. **大台、小台、微台互抵**：同月份的 TX、MXF、TMF 用可成交的掛單價（買一、賣一）比，扣掉手續費和期交稅後，淨利 ≥ 門檻，而且連續 ≥ 1 秒才通知。會看五檔深度，算出實際能成交幾組。
2. **選擇權到期價值的上下界**：用每檔成分股的漲跌停價推算加權指數今天的理論地板和天花板（`index_floor.py`），找出價外深到「到期一定歸零」或「一定價內」的履約價。bid 高於上界加成本就賣，ask 低於下界減成本就買。
3. **0050、正2、反1 等 ETF 對淨值、對期貨**（**不是無風險**）：
   - ETF 價格對發行商預估淨值的折溢價。
   - ETF 報酬 ÷ 倍數，和近月大台報酬比。
   - 同指數 ETF 之間互比。

完整邏輯、公式、門檻和成本假設，請看 [`LOGIC.md`](LOGIC.md)。

## 淨值怎麼算
- **監控程式（`monitor.py`）不自己算淨值**。每 15 秒抓證交所 `https://mis.twse.com.tw/stock/data/all_etf.txt`，用到的欄位：
  - `f` = 發行商預估淨值
  - `h` = 昨日淨值
  - `j` = 淨值時間
  - `e` = 成交價
- 折溢價一律用可成交價算：
  - 折價買進 = 賣一價 ÷ 預估淨值 − 1
  - 溢價賣出 = 買一價 ÷ 預估淨值 − 1
  - 扣掉成本後偏離 ≥ 1% × 倍數，而且連續 ≥ 5 秒才通知。
- **ETF 對大台**：(ETF 價 ÷ 昨日淨值 − 1) ÷ 倍數，對比 (大台價 ÷ 昨日結算價 − 1)。
- `scripts/etf_inav_record.py`：每 15 秒把所有 ETF 的預估淨值存成 CSV，留作研究資料。國外成分和主動式 ETF 事後重建不了盤中淨值，只能靠這個錄。
- 如果要自己重建淨值，研究時用過的公式：
  - 一般 ETF：盤中淨值 = 昨日淨值 × (1 + Σ PCF 權重 × 成分股報酬)，還沒成交的股票用昨收。
  - 槓桿、反向 ETF：盤中淨值 = 昨日淨值 × (1 + 倍數 × 台指期相對昨日結算價的報酬)。這類 ETF 是用期貨操作的，用現貨指數算在大波動日會差 1–2%。

## 安裝
```bash
pip install shioaji pandas pyarrow
cp config.example.json config.json      # 填 Telegram chat_id / thread_id，enabled 改 true；手續費改成你券商的費率
```
金鑰**不要放進 repo**（`.gitignore` 已經排除），以下兩種方式擇一：
- 環境變數：`SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY`、`TELEGRAM_BOT_TOKEN`
- 或在 repo 目錄放檔案：`.env.shioaji`（內含 `SHIOAJI_API_KEY=...`、`SHIOAJI_SECRET_KEY=...`），以及 `.env`（內含 `TELEGRAM_BOT_TOKEN=...`）

指數地板計算（`index_floor.py`）需要前一交易日的收盤價和市值，格式是 parquet，index 為日期、columns 為股票代號。放在 `data/close.parquet` 和 `data/mktcap.parquet`，或用 `FINLAB_RAW_DIR` 指定目錄。缺檔時會自動退回「昨收 × 0.9 / 1.1」，並加大 buffer。

## 執行
```bash
python -u monitor.py --dry                   # 只印、不發 Telegram
MON_TEST_S=60 python -u monitor.py --dry     # 跑 60 秒就結束（測試用）
python -u monitor.py                         # 正式
python replay_fut.py 20261106D               # 用錄下的 TXF/MXF 五檔回放互抵訊號（FUT_L2_RAW 指定資料目錄）
```
- 盤別：日盤 08:44–13:46（期貨、選擇權、ETF），夜盤 15:00–05:00（期貨互抵，加上隔天到期的選擇權）。
- 連線：Shioaji simulation 登入（行情是真實盤），只用一條連線，訂閱數 ≤ 200。注意同一個帳號最多 5 條連線。
- `scripts/arb_monitor_watchdog.sh`：給 Windows Git Bash 的 cron 用，每 5 分鐘檢查，沒在跑就用 WMI 背景啟動，中途斷線時每個盤別只通知一次。
- 輸出：`log/monitor.log`，以及 `log/opps_YYYYMMDD{D|N}.csv`（每次訊號都記錄）。

## 檔案
| 檔案 | 用途 |
|---|---|
| `monitor.py` | 主程式：登入、訂閱、淨值輪詢、Telegram 推播、記錄 |
| `engine.py` | 純計算：期貨互抵、選擇權上下界、ETF 折溢價訊號 |
| `index_floor.py` | 加權指數今日理論地板／天花板 |
| `replay_fut.py` | 用錄下的五檔回放期貨互抵訊號 |
| `config.example.json` | 參數範本（費率、門檻、ETF 清單、Telegram） |
| `scripts/etf_inav_record.py` | 每 15 秒錄全部 ETF 的預估淨值 |
| `scripts/arb_monitor_watchdog.sh` | cron watchdog |

## 注意
- 訊號只是通知，不會下單。兩邊報價到達的時間不一致，會造成一閃而過的假價差，所以程式要求訊號持續一段時間才發，但實際成交仍可能滑價或只成交一部分。
- 第 3 類（ETF）有追蹤誤差、基差和淨值估算延遲的風險，不是無風險套利。
- 手續費和期交稅是假設值，請改成你自己的實際費率。
- 本專案僅供研究，不構成投資建議。
