import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_indicators():
    code = "603986"
    print(f"--- Checking Main Indicators for {code} ---")
    try:
        df = ak.stock_main_indicator_em(symbol=code)
        if not df.empty:
             print("Latest row:")
             print(df.iloc[0]) # usually returns from newest to oldest
             print("\nColumns:", df.columns.tolist())
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_indicators()
