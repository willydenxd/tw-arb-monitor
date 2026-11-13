"""replay_fut.py TAG -- 用 tick_data/fut_l2 錄下的五檔回放『大小台互抵』訊號（只有 TXF/MXF，沒錄微台）."""
import sys, json, zlib, datetime as dt, collections
sys.path.insert(0, '.'); import engine
CFG = json.load(open('config.json', encoding='utf-8'))['fut']
TAG = sys.argv[1] if len(sys.argv) > 1 else '20261106D'
import os
BOOKS = os.environ.get('FUT_L2_RAW', 'fut_l2_raw')     # recorded TXF/MXF books: {BOOKS}/{TAG}/books.jsonl.gz
raw = open(f'{BOOKS}/{TAG}/books.jsonl.gz', 'rb').read()
d = zlib.decompressobj(16 + zlib.MAX_WBITS); out = []; data = raw
while data:
    try:
        out.append(d.decompress(data)); data = d.unused_data
        if data: d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    except Exception: break
recs = []
for l in b''.join(out).decode('utf-8', 'ignore').splitlines():
    try: r = json.loads(l)
    except Exception: continue
    if r.get('simtrade'): continue
    recs.append(r)
recs.sort(key=lambda r: r['datetime'])
codes = sorted(set(r['code'] for r in recs))
months = collections.defaultdict(dict)
for c in codes: months[c[3:]][c[:3]] = c
PAIRS = [(m['TXF'], m['MXF'], 4, 'TXF', 'MXF') for m in months.values() if 'TXF' in m and 'MXF' in m]
book = {}; msgs = []
def fmt(o): return f"{o['side']} {o['buy']}@{o['buy_px']:.0f}/{o['sell']}@{o['sell_px']:.0f} gross {o['gross']:.0f} net {o['net']:.1f} sets {o['size']} total NT${o['twd']:,.0f} {o['big_lim']}{o['small_lim']}"
AL = engine.Alerts(lambda m: msgs.extend((T, x) for x in m), fmt, {'fut': CFG['realert_s']}, persist={'fut': CFG['persist_s']})
best = collections.defaultdict(float); secs = collections.Counter()
lastsec = None
for r in recs:
    T = r['datetime']
    b = book.setdefault(r['code'], {})
    b.update(bids=list(zip(r['bid_price'], r['bid_volume'])), asks=list(zip(r['ask_price'], r['ask_volume'])))
    if r['code'].startswith(('TXFK6', 'MXFK6')): b['lim_dn'] = 37694.0
    opps = engine.fut_pair_edges(book, PAIRS, CFG)
    for o in opps:
        best[o['key']] = max(best[o['key']], o['net']); secs[(o['key'], T[:19])] += 1
    AL.update(opps, now=dt.datetime.fromisoformat(T).timestamp())
print('alerts', len(msgs))
for T, m in msgs[:40]: print(T[11:23], m)
print('best net by key', dict(best))
c = collections.Counter(k for k, s in secs)
print('seconds with an opportunity, by key', dict(c))
