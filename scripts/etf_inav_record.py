# etf_inav_record.py -- cron no_agent（平日 09:00-13:59 每分鐘）：抓證交所 mis 全部 ETF 預估淨值 4 次（每 15 秒），
# 追加到 inav_live/YYYYMMDD.csv（可用環境變數 INAV_OUT 改）。輸出永遠為空（不發訊息）。
# 用途：海外 / 槓桿 / 主動式 ETF 沒辦法事後重建盤中淨值，只能從現在開始錄。
import urllib.request, json, time, datetime as dt, os, csv
O = os.environ.get('INAV_OUT', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'inav_live')); os.makedirs(O, exist_ok=True)
for i in range(4):
    t0 = time.time(); now = dt.datetime.now()
    if not (dt.time(9, 0) <= now.time() <= dt.time(13, 35)) or now.weekday() >= 5:
        break
    try:
        r = urllib.request.urlopen(urllib.request.Request('https://mis.twse.com.tw/stock/data/all_etf.txt',
                                   headers={'User-Agent': 'Mozilla/5.0'}), timeout=10).read()
        j = json.loads(r.decode('utf-8', 'ignore'))
        f = f'{O}/{now:%Y%m%d}.csv'; new = not os.path.exists(f)
        with open(f, 'a', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            if new: w.writerow(['recv', 'code', 'name', 'px', 'inav', 'prem_pct', 'prev_nav', 'mis_date', 'mis_time', 'units', 'units_diff'])
            for b in j.get('a1', []):
                for m in b.get('msgArray', []) or []:
                    w.writerow([now.isoformat(timespec='seconds'), m.get('a'), m.get('b'), m.get('e'), m.get('f'), m.get('g'),
                                m.get('h'), m.get('i'), m.get('j'), m.get('c'), m.get('d')])
    except Exception:
        pass
    if i < 3: time.sleep(max(0, 15 - (time.time() - t0)))
