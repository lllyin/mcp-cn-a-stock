import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_indicator_lg():
    code = "603986" # 兆易创新
    print(f"--- AkShare stock_a_indicator_lg for {code} ---")
    try:
        # Get the latest row
        df = ak.stock_a_indicator_lg(symbol=code)
        if not df.empty:
            print(df.tail(1))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_indicator_lg()
