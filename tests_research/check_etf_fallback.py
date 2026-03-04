"""
验证脚本: 对于 efinance 无法查询的新 ETF (如 SH563230)，
测试自动 fallback 到 AkShare 的 fund_etf_hist_em 接口的方案可行性。

运行方式:
    cd /Users/desongan/3l-workspace/mcp-cn-a-stock
    .venv/bin/python3 tests_research/check_etf_fallback.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(override=True)

import akshare_proxy_patch
from qtf_mcp.config import AKSHARE_PROXY_IP, AKSHARE_PROXY_PASSWORD, AKSHARE_PROXY_PORT

print(f"[proxy] IP={AKSHARE_PROXY_IP} PORT={AKSHARE_PROXY_PORT}")
akshare_proxy_patch.install_patch(AKSHARE_PROXY_IP, AKSHARE_PROXY_PASSWORD, AKSHARE_PROXY_PORT)

import efinance as ef
import akshare as ak
from datetime import datetime, timedelta

START = "20260224"
END = datetime.now().strftime("%Y%m%d")

# ── 测试标的 ──────────────────────────────────────────────
# 513980: 老ETF，efinance可查；563230: 新ETF(2025-09上市)，efinance查不到
CASES = [
    ("513980", "港股科技50 SH513980 (老ETF，ef.stock可查)"),
    ("563230", "卫星ETF SH563230 (2025新ETF，ef.stock返回空)"),
]

# ── fallback 函数（模拟 _fetch_kline_sync 逻辑）────────────
def fetch_kline_with_fallback(code: str, start: str, end: str, fqt: int = 1):
    """
    先用 efinance ef.stock 查，如果返回空则 fallback 到 akshare fund_etf_hist_em
    """
    adjust_map = {0: "none", 1: "qfq", 2: "hfq"}
    ak_adjust = adjust_map.get(fqt, "qfq")

    # Step 1: efinance
    df = ef.stock.get_quote_history(code, beg=start, end=end, fqt=fqt)
    if df is not None and not df.empty:
        print(f"  [efinance] ✅ 成功, 行数={len(df)}")
        return df, "efinance"

    # Step 2: fallback to AkShare fund_etf_hist_em
    print(f"  [efinance] ❌ 返回空，尝试 AkShare fund_etf_hist_em fallback...")
    try:
        df_ak = ak.fund_etf_hist_em(symbol=code, period="daily",
                                     start_date=start, end_date=end, adjust=ak_adjust)
        if df_ak is not None and not df_ak.empty:
            # akshare 列名与 efinance 一致性检查
            print(f"  [akshare]  ✅ 成功, 行数={len(df_ak)}, 列={df_ak.columns.tolist()}")
            return df_ak, "akshare_fallback"
        else:
            print(f"  [akshare]  ❌ 也返回空")
            return None, "none"
    except Exception as e:
        print(f"  [akshare]  ❌ 报错: {e}")
        return None, "error"


# ── 主测试 ───────────────────────────────────────────────
print("=" * 65)
print(f"测试日期范围: {START} ~ {END}")
print("=" * 65)

results = {}
for code, desc in CASES:
    print(f"\n{'─'*60}")
    print(f"标的: {desc}")
    df, source = fetch_kline_with_fallback(code, START, END)
    results[code] = (df, source)

    if df is not None and not df.empty:
        print(f"\n  数据预览(最近3行):")
        print(df.head(3).to_string(index=False))
        print(f"\n  列名: {df.columns.tolist()}")

# ── 数据字段对比 ──────────────────────────────────────────
print(f"\n{'='*65}")
print("列名一致性对比:")
for code, (df, source) in results.items():
    if df is not None:
        print(f"  {code} ({source}): {df.columns.tolist()}")

# ── 关键列是否一致 ────────────────────────────────────────
REQUIRED_COLS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "涨跌幅", "换手率"]
print(f"\n必要列检查 {REQUIRED_COLS}:")
for code, (df, source) in results.items():
    if df is not None:
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            print(f"  {code} ({source}): ❌ 缺少 {missing}")
        else:
            print(f"  {code} ({source}): ✅ 全部字段齐全")
    else:
        print(f"  {code}: ❌ 无数据")

# ── 单位验证(对于同时能查到的 513980) ────────────────────
print(f"\n{'='*65}")
print("单位一致性验证 (同一天 513980 的 ef vs ak 比较):")
code_ref = "513980"
df_ef = ef.stock.get_quote_history(code_ref, beg=START, end=END, fqt=1)
try:
    df_ak = ak.fund_etf_hist_em(symbol=code_ref, period="daily",
                                 start_date=START, end_date=END, adjust="qfq")
    if df_ef is not None and not df_ef.empty and df_ak is not None and not df_ak.empty:
        # 取相同日期
        ef_last = df_ef.iloc[0]
        ak_last = df_ak[df_ak["日期"] == ef_last["日期"]]
        if not ak_last.empty:
            ak_row = ak_last.iloc[0]
            print(f"\n  日期: {ef_last['日期']}")
            print(f"  {'字段':<10} {'efinance':>15} {'akshare':>15} {'一致?':>6}")
            print(f"  {'-'*48}")
            for col in ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "涨跌幅"]:
                if col in df_ak.columns:
                    ev, av = ef_last[col], ak_row[col]
                    match = "✅" if abs(float(ev) - float(av)) < 0.01 else "❌"
                    print(f"  {col:<10} {float(ev):>15.4f} {float(av):>15.4f} {match:>6}")
        else:
            print("  日期未对齐，无法比较")
except Exception as e:
    print(f"  akshare 比较失败: {e}")

print(f"\n{'='*65}")
print("结论生成完毕。")
