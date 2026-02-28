import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_ak_valuation():
    code = "603986"
    print(f"--- Checking AkShare Valuation for {code} ---")
    try:
        # stock_a_lg_indicator is now deprecated, use stock_individual_info_em or others
        # Actually em real-time data might have it.
        # Let's try stock_zh_a_spot_em which is often used for real-time
        import efinance as ef
        rt = ef.stock.get_realtime_quotes()
        match = rt[rt['股票代码'] == code]
        print("Columns Available:", rt.columns.tolist())
        print(match.iloc[0])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_ak_valuation()
