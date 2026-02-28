import efinance as ef
import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def debug_valuation_data():
    code = "603986" # 兆易创新
    print(f"--- Debugging Valuation Data for {code} ---")
    
    # Financial data from akshare
    df_fin = ak.stock_financial_abstract_ths(symbol=code, indicator="主要指标")
    if not df_fin.empty:
        print("\n[AkShare Financial Abstract (Last 5 periods)]")
        # 字段: 报告期, 净利润, ...
        # Note: Columns might vary.
        # Let's see columns
        print("Columns:", df_fin.columns.tolist())
        target_cols = [c for c in df_fin.columns if any(k in c for k in ["报告期", "净利润", "每股净资产", "动态市盈率"])]
        print(df_fin[target_cols].head())

    # Snapshot for total market val
    info = ef.stock.get_base_info(code)
    tcap = info.get("总市值", 0)
    print(f"\nTotal Market Cap (efinance): {tcap}")
    
    # Try dynamic PE from realtime
    rt = ef.stock.get_realtime_quotes()
    match = rt[rt['股票代码'] == code]
    if not match.empty:
        print(f"Dynamic PE (efinance): {match.iloc[0]['动态市盈率']}")

if __name__ == "__main__":
    debug_valuation_data()
