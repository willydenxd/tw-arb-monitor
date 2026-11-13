"""engine.py -- 套利訊號的純計算（不碰網路，方便用錄好的資料回放測試）.

三類：
  1. fut_pairs   大台/小台/微台 同月份互抵：買便宜那邊、賣貴那邊，同一個最後結算價 → 價差到期必歸零。
  2. opt_bounds  選擇權到期價值上下界：結算價 S 一定落在 [S_lo, S_hi]（漲跌停 + 結算均價已觀測部分），
                 Call ∈ [max(0,S_lo-K), max(0,S_hi-K)]、Put ∈ [max(0,K-S_hi), max(0,K-S_lo)]；
                 bid > 上界 → 賣（必賺）；ask < 下界 → 買（必賺）。
  3. etf_rel     0050 / 正2 / 反1 vs 台指期、ETF 之間 2:1 關係、ETF vs 發行商預估淨值。（有基差/追蹤誤差風險，不是無風險）
"""
import math, time

# ---------------------------------------------------------------- 1. futures pairs
def _take(levels, q0, q):
    """levels=[(px, vol)...] 最佳在前；吃掉第 q0+1..q0+q 口的總價，深度不夠回 None。"""
    tot, skip, need = 0.0, q0, q
    for px, v in levels:
        if px <= 0 or v <= 0: continue
        if skip >= v: skip -= v; continue
        use = min(v - skip, need); skip = 0
        tot += use * px; need -= use
        if need == 0: return tot
    return None

def _one_dir(buyL, sellL, ratio_buy, ratio_sell, cost, mn, max_sets=50):
    """逐組加：第 k 組 = 買 ratio_buy 口 + 賣 ratio_sell 口。回傳 (sets, 第一組毛價差點, 平均淨點)。"""
    sets, first, tot_net = 0, None, 0.0
    for k in range(max_sets):
        pb = _take(buyL, k * ratio_buy, ratio_buy); ps = _take(sellL, k * ratio_sell, ratio_sell)
        if pb is None or ps is None: break
        g = ps / ratio_sell - pb / ratio_buy
        if g - cost < mn: break
        if first is None: first = g
        sets += 1; tot_net += g - cost
    return sets, first, (tot_net / sets if sets else 0.0)

def fut_pair_edges(books, pairs, cfg):
    """books: code -> dict(bids=[(px,vol)]*5, asks=[...], lim_dn, lim_up)。pairs: [(big, small, ratio, big_root, small_root)]。
    一組 = 1 口大 + ratio 口小；吃五檔深度算真的能成交的組數。edge 單位 = 指數點。"""
    out = []
    fee, tax, mult, mn = cfg['fee_twd'], cfg['tax'], cfg['mult'], cfg['min_net_pts']
    for big, small, ratio, rb, rs in pairs:
        A, B = books.get(big), books.get(small)
        if not A or not B or not A.get('asks') or not B.get('bids'):
            continue
        px = (A['asks'][0][0] or A['bids'][0][0]) if A.get('asks') else 0
        if not px:
            continue
        # 來回成本（點，每組）：期交稅 2 腿 × 開平 2 次 + 手續費 (1 大 + ratio 小) × 2 次
        cost = 4 * tax * px + (fee[rb] + ratio * fee[rs]) * 2 / mult[rb]
        for side, buyB, sellB, rbuy, rsell, buy, sell in (('買大賣小', A['asks'], B['bids'], 1, ratio, big, small),
                                                           ('賣大買小', B['asks'], A['bids'], ratio, 1, small, big)):
            n, g, avg = _one_dir(buyB, sellB, rbuy, rsell, cost, mn)
            if n:
                out.append(dict(kind='fut', key=f"fut|{big}/{small}|{'buyBIG' if side == '買大賣小' else 'sellBIG'}",
                                big=big, small=small, ratio=ratio, side=side, buy=buy, buy_px=buyB[0][0], sell=sell,
                                sell_px=sellB[0][0], gross=g, cost=cost, net=g - cost, avg_net=avg, size=n,
                                twd=avg * n * mult[rb], big_lim=_limflag(A), small_lim=_limflag(B)))
    return out

def _limflag(b):
    for k in ('bids', 'asks'):
        p = b[k][0][0] if b.get(k) else 0
        if p and b.get('lim_dn') and abs(p - b['lim_dn']) < 1e-6: return '跌停'
        if p and b.get('lim_up') and abs(p - b['lim_up']) < 1e-6: return '漲停'
    return ''

# ---------------------------------------------------------------- 2. option bounds
class SettleTracker:
    """到期日結算價界線：13:00(不含)~13:25(含) 每 5 秒一筆 + 13:30 收盤指數，共 301 筆簡單平均。"""
    N = 301
    def __init__(self, floor, ceil, round_pts=2):
        self.floor, self.ceil, self.rp = floor, ceil, round_pts
        self.slots = {}                       # slot(1..300, 301=close) -> value

    def on_index(self, hhmmss_s, value):
        """hhmmss_s = 當天秒數（交易所時間）。"""
        t0 = 13 * 3600
        if hhmmss_s >= 13 * 3600 + 30 * 60:   # 13:30 收盤指數
            self.slots[301] = value; return
        if t0 < hhmmss_s <= t0 + 25 * 60:
            k = int(math.ceil((hhmmss_s - t0) / 5.0))
            self.slots[min(max(k, 1), 300)] = value

    def bounds(self):
        n = len(self.slots); s = sum(self.slots.values())
        lo = (s + (self.N - n) * self.floor) / self.N
        hi = (s + (self.N - n) * self.ceil) / self.N
        return lo - self.rp, hi + self.rp, n


def opt_payoff_bounds(right, K, S_lo, S_hi):
    if right == 'C':
        return max(0.0, S_lo - K), max(0.0, S_hi - K)
    return max(0.0, K - S_hi), max(0.0, K - S_lo)


def opt_edges(books, meta, bounds_by_exp, cfg):
    """meta: code -> dict(K, right, exp). bounds_by_exp: exp(date str) -> (S_lo, S_hi, note)."""
    out = []
    fee_pt = cfg['fee_twd'] / 50.0; tax = cfg['tax']; mn = cfg['min_net_pts']
    for code, b in books.items():
        m = meta.get(code)
        if not m or m['exp'] not in bounds_by_exp:
            continue
        S_lo, S_hi, note = bounds_by_exp[m['exp']]
        lo, hi = opt_payoff_bounds(m['right'], m['K'], S_lo, S_hi)
        bid, ask = b.get('bid') or 0, b.get('ask') or 0
        if bid > 0 and b.get('bv', 0) > 0:
            net = bid - hi - fee_pt - tax * bid - (fee_pt if hi > 0 else 0)
            if net >= mn:
                out.append(dict(kind='opt', key=f'opt|{code}|sell', code=code, side='賣出', px=bid, size=b['bv'],
                                lo=lo, hi=hi, net=net, twd=net * 50, K=m['K'], right=m['right'], exp=m['exp'],
                                S_lo=S_lo, S_hi=S_hi, note=note))
        if ask > 0 and b.get('av', 0) > 0:
            net = lo - ask - fee_pt - tax * ask - (fee_pt if lo > 0 else 0)
            if net >= mn:
                out.append(dict(kind='opt', key=f'opt|{code}|buy', code=code, side='買進', px=ask, size=b['av'],
                                lo=lo, hi=hi, net=net, twd=net * 50, K=m['K'], right=m['right'], exp=m['exp'],
                                S_lo=S_lo, S_hi=S_hi, note=note))
    return out

# ---------------------------------------------------------------- 3. ETF relative value
def _ret(px, base):
    return px / base - 1 if px and base else None

def etf_edges(stk, nav, fut_book, fut_ref, cfg):
    """stk: code -> dict(bid, ask, bv, av, ref). nav: code -> dict(nav, prev_nav, t). fut_book: 近月 TXF book."""
    out = []; E = cfg['codes']
    cost_pct = 2 * cfg['fee_pct'] * cfg['fee_discount'] + cfg['tax_pct']      # ETF 一買一賣（%）
    # (a) 價格 vs 發行商預估淨值
    for c, (L, idx) in E.items():
        s, n = stk.get(c), nav.get(c)
        if not s or not n or not n.get('nav'):
            continue
        thr = cfg['prem_alert_pct'] * max(1, abs(L))
        if s.get('ask') and s.get('av', 0) > 0:
            p = (s['ask'] / n['nav'] - 1) * 100
            if -p - cost_pct >= thr:
                out.append(dict(kind='etf', key=f'nav|{c}|buy', code=c, side='折價買進', px=s['ask'], nav=n['nav'],
                                pct=p, net_pct=-p - cost_pct, nav_t=n.get('t', ''), L=L))
        if s.get('bid') and s.get('bv', 0) > 0:
            p = (s['bid'] / n['nav'] - 1) * 100
            if p - cost_pct >= thr:
                out.append(dict(kind='etf', key=f'nav|{c}|sell', code=c, side='溢價賣出', px=s['bid'], nav=n['nav'],
                                pct=p, net_pct=p - cost_pct, nav_t=n.get('t', ''), L=L))
    # (b) ETF vs 台指期（用昨日淨值當基準，避免昨天的折溢價污染）
    if fut_book and fut_ref:
        fa, fb = fut_book.get('ask'), fut_book.get('bid')
        for c, (L, idx) in E.items():
            s = stk.get(c)
            if not s:
                continue
            base = (nav.get(c) or {}).get('prev_nav') or s.get('ref')
            thr = cfg['vs_fut_alert_pct'][idx]
            # 期貨便宜：買 TX（ask），ETF 這邊要「賣出曝險」：L>0 賣 ETF(bid)，L<0 買 ETF(ask)
            ex = s.get('bid') if L > 0 else s.get('ask')
            rf = _ret(fa, fut_ref); re = _ret(ex, base)
            if rf is not None and re is not None and ((s.get('bv', 0) > 0) if L > 0 else (s.get('av', 0) > 0)):
                d = (re / L - rf) * 100
                if d - cost_pct / abs(L) >= thr:
                    out.append(dict(kind='etfut', key=f'efut|{c}|futcheap', code=c, side=f"買台指期 + {'賣' if L > 0 else '買'} {c}",
                                    etf_px=ex, fut_px=fa, r_etf=re * 100, r_fut=rf * 100, dev=d, idx=idx, L=L))
            ex = s.get('ask') if L > 0 else s.get('bid')
            rf = _ret(fb, fut_ref); re = _ret(ex, base)
            if rf is not None and re is not None and ((s.get('av', 0) > 0) if L > 0 else (s.get('bv', 0) > 0)):
                d = (rf - re / L) * 100
                if d - cost_pct / abs(L) >= thr:
                    out.append(dict(kind='etfut', key=f'efut|{c}|futrich', code=c, side=f"賣台指期 + {'買' if L > 0 else '賣'} {c}",
                                    etf_px=ex, fut_px=fb, r_etf=re * 100, r_fut=rf * 100, dev=d, idx=idx, L=L))
    # (c) ETF 之間（同指數或 TW50/TAIEX 交叉）：r_A / L_A vs r_B / L_B
    for a, b in cfg['pairs']:
        sa, sb = stk.get(a), stk.get(b)
        if not sa or not sb:
            continue
        La, Lb = E[a][0], E[b][0]
        ba = (nav.get(a) or {}).get('prev_nav') or sa.get('ref'); bb = (nav.get(b) or {}).get('prev_nav') or sb.get('ref')
        thr = cfg['pair_alert_pct'] if E[a][1] == E[b][1] else cfg['vs_fut_alert_pct']['TW50']
        for rich, cheap, sr, sc, Lr, Lc, br, bc in ((a, b, sa, sb, La, Lb, ba, bb), (b, a, sb, sa, Lb, La, bb, ba)):
            if not (sr.get('bid') and sr.get('bv', 0) > 0 and sc.get('ask') and sc.get('av', 0) > 0):
                continue
            rr, rc = _ret(sr['bid'], br) / Lr, _ret(sc['ask'], bc) / Lc
            d = (rr - rc) * 100
            if d - cost_pct >= thr:
                out.append(dict(kind='etfpair', key=f'pair|{rich}|{cheap}', code=f'{rich}/{cheap}',
                                side=f'賣 {rich}（{sr["bid"]}）買 {cheap}（{sc["ask"]}），金額比 {abs(Lc)}:{abs(Lr)}',
                                dev=d, r_rich=rr * Lr * 100, r_cheap=rc * Lc * 100))
    return out

# ---------------------------------------------------------------- alert manager
class Alerts:
    """同一個 key 要連續存在 persist 秒才發（濾掉兩個商品報價先後到的假價差）；
    之後 net 比上次發的大 50% 或每 realert 秒再發一次；消失 gap 秒後，發過的才發結束摘要。"""
    def __init__(self, send, fmt, realert, persist=None, gap=3.0):
        self.send, self.fmt, self.realert = send, fmt, realert
        self.persist = persist or {}; self.gap = gap
        self.st = {}; self.prev = set()

    def update(self, opps, now=None):
        now = now or time.time(); msgs = []; seen = set()
        for o in opps:
            k = o['key']; seen.add(k); v = _score(o)
            s = self.st.get(k)
            if s is None:
                s = self.st[k] = dict(first=now, run=now, last_seen=now, max=v, sent=False, sent_v=0, sent_t=0)
            if k not in self.prev: s['run'] = now          # 中斷過 → 連續存在時間重算
            s['last_seen'] = now; s['max'] = max(s['max'], v)
            if not s['sent']:
                if now - s['run'] >= self.persist.get(o['kind'], 0):
                    s.update(sent=True, sent_v=v, sent_t=now)
                    msgs.append('🚨 ' + self.fmt(o))
            elif (v >= 1.5 * s['sent_v'] and v > s['sent_v']) or now - s['sent_t'] >= self.realert.get(o['kind'], 120):
                s.update(sent_v=v, sent_t=now)
                msgs.append(f'⏳ 已持續 {now - s["first"]:.0f}s ' + self.fmt(o))
        for k in list(self.st):
            s = self.st[k]
            if k not in seen and now - s['last_seen'] > self.gap:
                if s['sent']:
                    msgs.append(f'✅ 結束 {k.split("|", 1)[1]}：持續 {s["last_seen"] - s["first"]:.0f}s，最大 {s["max"]:.1f}')
                del self.st[k]
        self.prev = seen
        if msgs:
            self.send(msgs)
        return msgs

def _score(o):
    return o.get('net', o.get('net_pct', o.get('dev', 0)))
