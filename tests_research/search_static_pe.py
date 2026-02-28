import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def search_static_pe():
    print("--- Searching for Static PE in AkShare ---")
    try:
        # Get one page of spot data to check columns
        df = ak.stock_zh_a_spot_em()
        if not df.empty:
            pe_cols = [c for c in df.columns if "市盈率" in c]
            print("Found PE related columns:", pe_cols)
            # Find a specific stock to see the values
            match = df[df['名称'] == "兆易创新"]
            if not match.empty:
                print("\nValues for 兆易创新:")
                print(match[pe_cols])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    search_static_pe()
