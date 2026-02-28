import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_indicators_new():
    code = "603986" # SH603986
    print(f"--- AkShare stock_zh_a_indicator_em for {code} ---")
    try:
        # This one is usually the go-to for PE/PB
        df = ak.stock_zh_a_indicator_em(symbol=code)
        if not df.empty:
            print(df.tail())
            print("\nColumns:", df.columns.tolist())
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_indicators_new()
