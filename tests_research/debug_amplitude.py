import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def debug_amplitude():
    code = "513980"
    # Get hist data for 2026-02-26 and 2026-02-27
    df = ef.stock.get_quote_history(code, beg='20260226', end='20260227', fqt=0)
    print("--- Historical Data (Unadjusted) ---")
    if not df.empty:
        # Columns in efinance quote history: 股票名称, 股票代码, 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        print(df[['日期', '收盘', '最高', '最低', '振幅']])
        
        row_27 = df[df['日期'] == '2026-02-27']
        row_26 = df[df['日期'] == '2026-02-26']
        
        if not row_27.empty and not row_26.empty:
            c26 = row_26.iloc[0]['收盘']
            h27 = row_27.iloc[0]['最高']
            l27 = row_27.iloc[0]['最低']
            amp27 = row_27.iloc[0]['振幅']
            
            calc = (h27 - l27) / c26 * 100
            print(f"\nPrevious Close (2026-02-26): {c26}")
            print(f"Current High   (2026-02-27): {h27}")
            print(f"Current Low    (2026-02-27): {l27}")
            print(f"Reported Amplitude: {amp27}%")
            print(f"Calculated ((H-L)/PrevClose): {calc:.3f}%")
            
            # Alt calculation hypothesis: (H-L)/L?
            calc_alt = (h27 - l27) / l27 * 100
            print(f"Alt Hypothesis ((H-L)/L): {calc_alt:.3f}%")

if __name__ == "__main__":
    debug_amplitude()
