"""index_floor.py -- 加權指數「今日理論地板 / 天花板」.

TAIEX = Σ(價格 × 股數) / 除數（價格指數，除息不調除數）。每檔上市股今天最多跌到跌停價 LD_i、
最多漲到漲停價 LU_i，所以今天任何時點的指數都落在
    floor = 昨收指數 × Σ mv_i × (LD_i / C_i) / Σ mv_i
    ceil  = 昨收指數 × Σ mv_i × (LU_i / C_i) / Σ mv_i
其中 C_i = 昨日收盤（未除息）、mv_i = 昨日市值、LD/LU 取永豐合約檔（已含除權息參考價與跳動單位進位）。
跟「昨收 × 0.9」的差別：除權息點數、跌停價往上進位到跳動單位。

資料：FinLab 本機快取 features/raw/close.parquet、mktcap.parquet（夜間 pipeline 更新），
最後一天必須 = 前一交易日，否則退回 0.9/1.1 並加大 buffer（呼叫端處理）。
"""
import datetime as dt
import pandas as pd

import os
RAW = os.environ.get('FINLAB_RAW_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))   # close.parquet / mktcap.parquet (index=date, columns=stock id)


def compute(api, idx_ref, prev_date=None):
    """回傳 dict(floor, ceil, ratio_dn, ratio_up, n, cover, data_date, exact)。
    api = 已登入的 shioaji；idx_ref = 加權指數參考價（昨收）。"""
    close = pd.read_parquet(f'{RAW}/close.parquet')
    mv = pd.read_parquet(f'{RAW}/mktcap.parquet')
    d = min(close.index[-1], mv.index[-1])
    C = close.loc[d]; M = mv.loc[d]
    rows = []
    for c in api.Contracts.Stocks.TSE:
        code = c.code
        if len(code) != 4 or not code.isdigit() or code.startswith('0') or code.startswith('91'):
            continue                                  # 只留普通股（排除 ETF/TDR/特別股）
        if code not in M.index or code not in C.index:
            continue
        m, cl = M.get(code), C.get(code)
        if not (m > 0 and cl > 0):
            continue
        ld, lu, ref = float(c.limit_down or 0), float(c.limit_up or 0), float(c.reference or 0)
        if ld <= 0 or lu <= 0 or ref <= 0:
            continue
        if getattr(c, 'trading_suspended', False):    # 停牌股價格不動
            ld = lu = cl
        rows.append((code, m, cl, ref, ld, lu))
    df = pd.DataFrame(rows, columns=['code', 'mv', 'close', 'ref', 'ld', 'lu'])
    # 新上市前 5 日無漲跌幅：合約檔的 ld/lu 會離 ref 很遠；比例太怪的直接當 0 / 2 倍（保守）
    df['rd'] = (df.ld / df.close).clip(upper=1.0)
    df['ru'] = (df.lu / df.close).clip(lower=1.0)
    odd = (df.rd < 0.85) | (df.ru > 1.15)
    df.loc[odd & (df.rd < 0.85), 'rd'] = 0.0
    df.loc[odd & (df.ru > 1.15), 'ru'] = 2.0
    w = df.mv / df.mv.sum()
    rdn, rup = float((w * df.rd).sum()), float((w * df.ru).sum())
    exdiv = float((w * (1 - df.ref / df.close)).sum())   # 除權息造成的指數跌幅（比例）
    exact = prev_date is None or d.date() == prev_date
    return dict(floor=idx_ref * rdn, ceil=idx_ref * rup, ratio_dn=rdn, ratio_up=rup, n=len(df),
                n_odd=int(odd.sum()), exdiv_pct=exdiv * 100, data_date=str(d.date()), exact=bool(exact))


if __name__ == '__main__':
    import os, sys, json
    import shioaji as sj
    K = {}
    for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env.shioaji'), encoding='utf-8'):
        l = l.strip()
        if '=' in l and not l.startswith('#'):
            a, b = l.split('=', 1); K[a.strip()] = b.strip()
    api = sj.Shioaji(simulation=True)
    api.login(api_key=K['SHIOAJI_API_KEY'], secret_key=K['SHIOAJI_SECRET_KEY'], subscribe_trade=False)
    try:
        ix = [c for c in api.Contracts.Indexs.TSE if c.code == 'IX0001'][0]
        r = compute(api, float(ix.reference))
        r['idx_ref'] = float(ix.reference); r['naive_floor'] = ix.reference * 0.9; r['naive_ceil'] = ix.reference * 1.1
        print(json.dumps(r, ensure_ascii=False, indent=1))
    finally:
        api.logout(); sys.stdout.flush(); os._exit(0)
