import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def get_pb_hist():
    code = "603986"
    print(f"--- AkShare stock_a_pb_em for {code} ---")
    try:
        # Note: some functions use symbols like 'sh603986'
        df = ak.stock_a_pb_em(symbol=code)
        if not df.empty:
            print(df.tail())
            print("\nColumns:", df.columns.tolist())
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    get_pb_hist()
