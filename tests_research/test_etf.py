import akshare as ak
import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def test_etf_akshare(code="513980"):
    print(f"\n--- Testing AkShare for ETF {code} ---")
    try:
        # Method 1: Use general stock hist (often doesn't work for ETFs)
        print("Trying ak.stock_zh_a_hist...")
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20260225", end_date="20260227")
        if df is not None and not df.empty:
            print("Success with stock_zh_a_hist!")
            print(df.head(2))
        else:
            print("Failed with stock_zh_a_hist.")

        # Method 2: Use dedicated ETF hist
        print("Trying ak.fund_etf_hist_em...")
        df_etf = ak.fund_etf_hist_em(symbol=code, period="daily", start_date="20260225", end_date="20260227", adjust="qfq")
        if df_etf is not None and not df_etf.empty:
            print("Success with fund_etf_hist_em!")
            print(df_etf.head(2))
    except Exception as e:
        print(f"AkShare Error: {e}")

def test_etf_efinance(code="513980"):
    print(f"\n--- Testing efinance for ETF {code} ---")
    try:
        # efinance uses the same interface for stars, stocks, and ETFs
        print("Trying ef.stock.get_quote_history...")
        df = ef.stock.get_quote_history(code, beg='20260225', end='20260227')
        if df is not None and not df.empty:
            print("Success with efinance!")
            print(df.head(2))
        
        print("\nTrying ef.stock.get_base_info (Real-time)...")
        info = ef.stock.get_base_info(code)
        print(info)
    except Exception as e:
        print(f"efinance Error: {e}")

if __name__ == "__main__":
    test_etf_akshare()
    test_etf_efinance()
