import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def find_pb():
    print("--- Searching for PB in AkShare Spot Data ---")
    try:
        df = ak.stock_zh_a_spot_em()
        if not df.empty:
            print("Columns Found:", df.columns.tolist())
            # Searching for "市净率"
            target_cols = [c for c in df.columns if "市净率" in c]
            print("\nTarget Columns:", target_cols)
            if target_cols:
                # Get for SH603986
                match = df[df['代码'] == '603986']
                if not match.empty:
                    print("\nValues for 603986:")
                    print(match[['代码', '名称'] + target_cols])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    find_pb()
