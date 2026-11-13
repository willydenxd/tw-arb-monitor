"""monitor.py -- 免費撿錢監控（大小微台互抵 / 選擇權到期上下界 / 0050·正2·反1 vs 期貨）.
用法: python -u monitor.py            （正式：發 Telegram）
      python -u monitor.py --dry      （只印不發）
      MON_TEST_S=60 python -u monitor.py --dry   （跑 60 秒就結束，測試用）
盤別: 日盤 08:44-13:46（期貨 + 選擇權 + ETF）；夜盤 15:00-05:00（期貨互抵 + 「下一交易日到期」選擇權）。
連線: 永豐 Shioaji simulation 登入（行情是真實盤），一條連線、訂閱 ≤ 200。
輸出: log/monitor.log、log/opps_YYYYMMDD{D|N}.csv（每次訊號都記，研究用）、Telegram。
"""
import os, sys, json, time, threading, datetime as dt, urllib.request, urllib.parse, traceback, csv
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace"); sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = HERE
sys.path.insert(0, HERE)
import engine, index_floor

CFG = json.load(open(os.environ.get('MON_CFG') or os.path.join(HERE, 'config.json'), encoding='utf-8'))
DRY = '--dry' in sys.argv
TEST_S = float(os.environ.get('MON_TEST_S', '0') or 0)
os.makedirs(os.path.join(HERE, 'log'), exist_ok=True)
LOGF = os.path.join(HERE, 'log', 'monitor.log')

def log(*a):
    s = f'[{dt.datetime.now():%m-%d %H:%M:%S}] ' + ' '.join(str(x) for x in a)
    print(s, flush=True); open(LOGF, 'a', encoding='utf-8').write(s + '\n')

def env(p):
    K = {}
    for l in open(p, encoding='utf-8'):
        l = l.strip()
        if '=' in l and not l.startswith('#'):
            a, b = l.split('=', 1); K[a.strip()] = b.strip().strip('"').strip("'")
    return K

# ---------------------------------------------------------------- telegram
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or (env(os.path.join(HERE, '.env')).get('TELEGRAM_BOT_TOKEN', '') if os.path.exists(os.path.join(HERE, '.env')) else '')
_tgq = []; _tglock = threading.Lock()

def tg_send_now(text):
    if DRY or not CFG['tg']['enabled'] or not TG_TOKEN:
        log('[TG-dry]', text.replace('\n', ' | ')); return True
    data = {'chat_id': CFG['tg']['chat_id'], 'text': text[:4000], 'disable_web_page_preview': 'true'}
    if CFG['tg'].get('thread_id'):
        data['message_thread_id'] = str(CFG['tg']['thread_id'])
    for i in range(3):
        try:
            req = urllib.request.Request(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                                         data=urllib.parse.urlencode(data).encode())
            urllib.request.urlopen(req, timeout=10).read(); return True
        except Exception as e:
            err = str(e)
            if 'thread' in err.lower() or '400' in err:     # thread 不存在 → 退回主聊天
                data.pop('message_thread_id', None)
            time.sleep(1 + i)
    log('TG send failed:', err); return False

def tg_worker():
    while True:
        time.sleep(1.0)
        with _tglock:
            batch = _tgq[:]; _tgq.clear()
        if batch:
            for i in range(0, len(batch), 12):           # 一則訊息最多 12 條，避免洗版 / 超長
                tg_send_now('\n\n'.join(batch[i:i + 12]))
                time.sleep(1.1)

def send(msgs):
    for m in msgs: log('ALERT', m.replace('\n', ' | '))
    with _tglock: _tgq.extend(msgs)

# ---------------------------------------------------------------- session
def session(now=None):
    now = now or dt.datetime.now(); t = now.time(); wd = now.weekday()
    if wd < 5 and dt.time(8, 30) <= t < dt.time(13, 46):
        return now.strftime('%Y%m%d') + 'D', now.replace(hour=13, minute=46, second=0, microsecond=0), False
    if wd < 5 and t >= dt.time(14, 50):
        return now.strftime('%Y%m%d') + 'N', (now + dt.timedelta(days=1)).replace(hour=5, minute=1, second=0, microsecond=0), True
    if 1 <= wd <= 5 and t < dt.time(5, 1):
        return (now - dt.timedelta(days=1)).strftime('%Y%m%d') + 'N', now.replace(hour=5, minute=1, second=0, microsecond=0), True
    return None

S = session()
if S is None and not TEST_S:
    log('not in session -> exit'); sys.exit(0)
if S is None:
    S = (dt.datetime.now().strftime('%Y%m%d') + 'T', dt.datetime.now() + dt.timedelta(seconds=TEST_S + 5), False)
TAG, END, NIGHT = S
if TEST_S: END = min(END, dt.datetime.now() + dt.timedelta(seconds=TEST_S))
SDAY = dt.datetime.strptime(TAG[:8], '%Y%m%d').date()

def next_weekday(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d

# ---------------------------------------------------------------- state
lock = threading.Lock()
fbook, obook, sbook = {}, {}, {}          # futures / options / stocks: code -> dict
nav = {}                                  # etf code -> dict(nav, prev_nav, t)
idx = {'last': None, 't': None}
SETTLE = None
last_msg = [time.time()]

def _f(x):
    try: return float(x)
    except Exception: return 0.0

def on_fop_bidask(*a):
    b = a[-1]
    if getattr(b, 'simtrade', False): return
    bids = [(_f(p), int(v)) for p, v in zip(b.bid_price, b.bid_volume)]; asks = [(_f(p), int(v)) for p, v in zip(b.ask_price, b.ask_volume)]
    d = dict(bid=bids[0][0], ask=asks[0][0], bv=bids[0][1], av=asks[0][1], bids=bids, asks=asks, t=str(b.datetime))
    with lock:
        tgt = fbook if b.code in FMETA else obook
        old = tgt.get(b.code, {})
        old.update(d); tgt[b.code] = old
    last_msg[0] = time.time()

def on_stk_bidask(*a):
    b = a[-1]
    if getattr(b, 'simtrade', False) or getattr(b, 'intraday_odd', False): return
    with lock:
        old = sbook.get(b.code, {})
        old.update(bid=_f(b.bid_price[0]), ask=_f(b.ask_price[0]), bv=int(b.bid_volume[0]), av=int(b.ask_volume[0]), t=str(b.datetime))
        sbook[b.code] = old
    last_msg[0] = time.time()

def on_idx(*a):
    q = a[-1]
    try:
        v = _f(q.close); tm = q.time if hasattr(q, 'time') else None
        secs = tm.hour * 3600 + tm.minute * 60 + tm.second if tm else None
    except Exception:
        return
    with lock:
        idx['last'], idx['t'] = v, secs
        if SETTLE is not None and secs is not None:
            SETTLE.on_index(secs, v)

# ---------------------------------------------------------------- iNAV poller (TWSE mis，15 秒)
def nav_worker():
    codes = set(CFG['etf']['codes'])
    while True:
        try:
            r = urllib.request.urlopen(urllib.request.Request('https://mis.twse.com.tw/stock/data/all_etf.txt',
                                       headers={'User-Agent': 'Mozilla/5.0'}), timeout=10).read()
            j = json.loads(r.decode('utf-8', 'ignore'))
            for blk in j.get('a1', []):
                for m in blk.get('msgArray', []) or []:
                    if m.get('a') in codes:
                        with lock:
                            nav[m['a']] = dict(nav=_f(m.get('f')), prev_nav=_f(m.get('h')), t=m.get('j', ''), px=_f(m.get('e')))
        except Exception as e:
            log('nav poll err', type(e).__name__, str(e)[:120])
        time.sleep(CFG['etf']['inav_poll_s'])

# ---------------------------------------------------------------- formatting
NAME = {'TXF': '大台', 'MXF': '小台', 'TMF': '微台'}
def fname(code): return NAME.get(code[:3], code[:3]) + code[3:]

def fmt(o):
    k = o['kind']
    if k == 'fut':
        lim = ' '.join(x for x in (o['big_lim'] and f"{fname(o['big'])}{o['big_lim']}", o['small_lim'] and f"{fname(o['small'])}{o['small_lim']}") if x)
        return (f"[期貨互抵] {o['side']}：買 {fname(o['buy'])}@{o['buy_px']:.0f} / 賣 {fname(o['sell'])}@{o['sell_px']:.0f}"
                f"（比例 1:{o['ratio']}）\n價差 {o['gross']:.0f} 點，扣成本 {o['cost']:.1f} → 淨 {o['net']:.1f} 點 ≈ NT${o['twd']:,.0f}/組，可成交 {o['size']} 組 {lim}\n"
                f"同月同結算價，價差到期必歸零；收斂就雙邊平倉")
    if k == 'opt':
        rt = '買權' if o['right'] == 'C' else '賣權'
        return (f"[選擇權上下界] {o['side']} {o['code']}（{o['exp']} {int(o['K'])}{rt}）@{o['px']}，量 {o['size']}\n"
                f"到期價值必在 [{o['lo']:.1f}, {o['hi']:.1f}]（結算價 ∈ [{o['S_lo']:.0f}, {o['S_hi']:.0f}] {o['note']}）→ 淨 {o['net']:.1f} 點 ≈ NT${o['twd']:,.0f}/口")
    if k == 'etf':
        return (f"[ETF 折溢價] {o['side']} {o['code']}@{o['px']}，預估淨值 {o['nav']}（{o['nav_t']}），{o['pct']:+.2f}%，扣成本 {o['net_pct']:.2f}%\n"
                f"注意：發行商淨值 15 秒更新且可能落後；要用期貨反向避險（{abs(o['L'])} 倍曝險）")
    if k == 'etfut':
        return (f"[ETF vs 台指期] {o['side']}：{o['code']} {o['r_etf']:+.2f}%（÷{o['L']}）vs 台指期 {o['r_fut']:+.2f}% → 偏離 {o['dev']:.2f}%\n"
                f"有基差 / {o['idx']} vs 加權 的追蹤風險，不是無風險；ETF 先賣需現沖或融券")
    if k == 'etfpair':
        return f"[ETF 倍數關係] {o['code']} 偏離 {o['dev']:.2f}%：{o['side']}（{o['r_rich']:+.2f}% vs {o['r_cheap']:+.2f}%）"
    return str(o)

OPP_F = os.path.join(HERE, 'log', f'opps_{TAG}.csv')
_ll = {}
def log_opps(opps):
    now = time.time()
    opps = [o for o in opps if _ll.get(o['key'], (None, 0))[0] != round(engine._score(o), 3) or now - _ll[o['key']][1] >= 5]
    if not opps: return
    for o in opps: _ll[o['key']] = (round(engine._score(o), 3), now)
    new = not os.path.exists(OPP_F)
    with open(OPP_F, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new: w.writerow(['ts', 'key', 'score', 'json'])
        ts = dt.datetime.now().isoformat(timespec='milliseconds')
        for o in opps: w.writerow([ts, o['key'], round(engine._score(o), 3), json.dumps(o, ensure_ascii=False, default=str)])

# ---------------------------------------------------------------- main
import shioaji as sj
K = {k: os.environ[k] for k in ('SHIOAJI_API_KEY', 'SHIOAJI_SECRET_KEY') if os.environ.get(k)} or env(os.path.join(HERE, '.env.shioaji'))
api = sj.Shioaji(simulation=True)
api.login(api_key=K['SHIOAJI_API_KEY'], secret_key=K['SHIOAJI_SECRET_KEY'], subscribe_trade=False)
log(f'start session={TAG} night={NIGHT} dry={DRY} until {END:%m-%d %H:%M}', 'usage', api.usage())
nsub = [0]
def sub(c, qt):
    api.subscribe(c, quote_type=qt, version=sj.QuoteVersion.v1); nsub[0] += 1
def unsub(c, qt):
    api.unsubscribe(c, quote_type=qt, version=sj.QuoteVersion.v1); nsub[0] -= 1

api.set_on_bidask_fop_v1_callback(on_fop_bidask)
api.set_on_bidask_stk_v1_callback(on_stk_bidask)
api.set_on_quote_idx_v1_callback(on_idx)

# --- futures
FMETA = {}; PAIRS = []
fc = CFG['fut']
bymonth = {}
for r in fc['roots']:
    cs = sorted([c for c in getattr(api.Contracts.Futures, r) if c.code[3:5] not in ('R1', 'R2')], key=lambda c: c.delivery_month)
    cs = [c for c in cs if (c.last_trading_date >= SDAY if not NIGHT else c.last_trading_date > SDAY)][:fc['months']]
    for c in cs:
        FMETA[c.code] = c; fbook[c.code] = dict(lim_dn=_f(c.limit_down), lim_up=_f(c.limit_up), ref=_f(c.reference))
        bymonth.setdefault(c.delivery_month, {})[r] = c.code
        sub(c, sj.QuoteType.BidAsk)
for mth, d in bymonth.items():
    for big, small in (('TXF', 'MXF'), ('TXF', 'TMF'), ('MXF', 'TMF')):
        if big in d and small in d:
            PAIRS.append((d[big], d[small], fc['mult'][big] // fc['mult'][small], big, small))
NEAR_TX = sorted([c for c in FMETA if c.startswith('TXF')], key=lambda c: FMETA[c].delivery_month)[0]
log('futures', list(FMETA), 'pairs', [(a, b) for a, b, *_ in PAIRS])

# --- index + floor/ceil
IX = [c for c in api.Contracts.Indexs.TSE if c.code == 'IX0001'][0]
oc = CFG['opt']
if not NIGHT:
    idx_ref = _f(IX.reference)
    prev_td = None
    try:
        fl = index_floor.compute(api, idx_ref)
        buf = oc['floor_buffer_pct'] / 100 * (1 if fl['exact'] else 4)
        FLOOR, CEIL = fl['floor'] * (1 - buf), fl['ceil'] * (1 + buf)
        log('floor/ceil', json.dumps(fl, ensure_ascii=False))
    except Exception as e:
        log('floor compute failed -> naive', e)
        FLOOR, CEIL = idx_ref * 0.9 * (1 - 0.003), idx_ref * 1.1 * (1 + 0.003)
    sub(IX, sj.QuoteType.Tick)
else:
    snap = api.snapshots([IX])[0]
    idx_ref = _f(snap.close)                 # 今天收盤 = 明天的參考
    b = oc['night_buffer_pct'] / 100         # 明天除權息還不知道 → 保守 buffer
    FLOOR, CEIL = idx_ref * 0.9 * (1 - b), idx_ref * 1.1 * (1 + b)
    log('night floor/ceil from close', idx_ref, FLOOR, CEIL)

# --- options: 日盤 = 今天到期；夜盤 = 下一交易日到期
OPT_EXP = SDAY if not NIGHT else next_weekday(SDAY)
ALLOPT = [c for cat in api.Contracts.Options for c in cat if getattr(c, 'underlying_code', '') == 'IX0001' and c.last_trading_date == OPT_EXP]
OMETA = {c.code: dict(K=float(c.strike_price), right=c.option_right.value, exp=str(c.last_trading_date), c=c) for c in ALLOPT}
SETTLE = engine.SettleTracker(FLOOR, CEIL, oc['settle_round_pts']) if (not NIGHT and OPT_EXP == SDAY) else None
osubs = set()

def opt_bounds():
    if SETTLE is not None:
        with lock: lo, hi, n = SETTLE.bounds()
        return {str(OPT_EXP): (lo, hi, f'已觀測 {n}/301 筆結算樣本' if n else '漲跌停界線')}
    return {str(OPT_EXP): (FLOOR, CEIL, '明日漲跌停界線（夜盤）' if NIGHT else '漲跌停界線')}

def rebalance_opts():
    if not OMETA: return
    lo, hi, _ = list(opt_bounds().values())[0]
    W = oc['strike_window_pts']
    def dist(m): return min(abs(m['K'] - lo), abs(m['K'] - hi))
    cand = sorted([k for k, m in OMETA.items() if dist(m) <= W], key=lambda k: dist(OMETA[k]))
    budget = min(oc['max_subs'], 195 - (nsub[0] - len(osubs)))
    want = set(cand[:budget])
    for k in list(osubs - want):
        try: unsub(OMETA[k]['c'], sj.QuoteType.BidAsk); osubs.discard(k)
        except Exception as e: log('unsub err', k, e)
        with lock: obook.pop(k, None)
    for k in sorted(want - osubs, key=lambda k: dist(OMETA[k])):
        try: sub(OMETA[k]['c'], sj.QuoteType.BidAsk); osubs.add(k)
        except Exception as e: log('sub err', k, e); break

# --- ETFs
if not NIGHT:
    for c in CFG['etf']['codes']:
        try:
            s = api.Contracts.Stocks[c]
            sbook[c] = dict(ref=_f(s.reference), lim_dn=_f(s.limit_down), lim_up=_f(s.limit_up))
            sub(s, sj.QuoteType.BidAsk)
        except Exception as e:
            log('etf sub err', c, e)
    threading.Thread(target=nav_worker, daemon=True).start()

rebalance_opts()
threading.Thread(target=tg_worker, daemon=True).start()
hello = (f"🟢 撿錢監控啟動 {TAG}：期貨 {len(FMETA)} 檔（{len(PAIRS)} 組互抵）、選擇權 {len(osubs)}/{len(OMETA)} 檔（到期 {OPT_EXP}）、"
         f"ETF {0 if NIGHT else len(CFG['etf']['codes'])} 檔，共訂閱 {nsub[0]}\n"
         f"加權參考 {idx_ref:.2f}，結算價界線 [{FLOOR:.1f}, {CEIL:.1f}]")
log(hello)
if not TEST_S or DRY: send([hello])

AL = engine.Alerts(send, fmt, {'fut': fc['realert_s'], 'opt': oc['realert_s'], 'etf': CFG['etf']['realert_s'],
                               'etfut': CFG['etf']['realert_s'], 'etfpair': CFG['etf']['realert_s']},
                   persist={'fut': fc['persist_s'], 'opt': oc['persist_s'], 'etf': CFG['etf']['persist_s'],
                            'etfut': CFG['etf']['persist_s'], 'etfpair': CFG['etf']['persist_s']})
last_reb = time.time(); last_rep = time.time(); code = 0
try:
    while True:
        time.sleep(0.25)
        now = dt.datetime.now()
        if now >= END: log('session end'); break
        with lock:
            fb = {k: dict(v) for k, v in fbook.items()}; ob = {k: dict(v) for k, v in obook.items()}
            sb = {k: dict(v) for k, v in sbook.items()}; nv = {k: dict(v) for k, v in nav.items()}
        opps = engine.fut_pair_edges(fb, PAIRS, fc)
        t = now.time()
        opt_live = (not NIGHT and dt.time(8, 45) <= t <= dt.time(13, 45)) or NIGHT
        if opt_live:
            opps += engine.opt_edges(ob, OMETA, opt_bounds(), oc)
        if not NIGHT and dt.time(9, 0, 30) <= t <= dt.time(13, 25):
            opps += engine.etf_edges(sb, nv, fb.get(NEAR_TX), fb.get(NEAR_TX, {}).get('ref'), CFG['etf'])
        log_opps(opps)
        AL.update(opps)
        if SETTLE is not None and dt.time(12, 59) <= t <= dt.time(13, 31) and time.time() - last_reb >= 15 \
                or time.time() - last_reb >= oc['rebalance_s']:
            rebalance_opts(); last_reb = time.time()
        if time.time() - last_rep > 1800:
            log('alive subs', nsub[0], 'fut', {k: (v.get('bid'), v.get('ask')) for k, v in fb.items()}, 'idx', idx['last'],
                'settle', SETTLE.bounds() if SETTLE else None); last_rep = time.time()
        in_trading = (not NIGHT and dt.time(8, 46) <= t <= dt.time(13, 44)) or (NIGHT and (t >= dt.time(15, 1) or t <= dt.time(4, 59)))
        if in_trading and time.time() - last_msg[0] > 600:
            log('no data 600s -> exit for restart'); code = 2; break
except KeyboardInterrupt:
    pass
except Exception:
    log('crash', traceback.format_exc()[-1500:]); code = 1
finally:
    time.sleep(2.5)                         # 讓 tg_worker 把最後一批送完
    log('final: subs', nsub[0], 'open alerts', list(AL.st))
    try: api.logout()
    except Exception: pass
    sys.stdout.flush(); os._exit(code)
