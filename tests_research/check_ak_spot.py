import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_ak_spot():
    code = "603986" # 兆易创新
    print(f"--- AkShare stock_zh_a_spot_em for {code} ---")
    df = ak.stock_zh_a_spot_em()
    if not df.empty:
        match = df[df['代码'] == code]
        if not match.empty:
             print("Columns Available:", df.columns.tolist())
             print(match.iloc[0])

if __name__ == "__main__":
    check_ak_spot()
