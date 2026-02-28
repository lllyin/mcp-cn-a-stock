import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_em_individual():
    code = "603986" # 兆易创新
    print(f"--- AkShare Individual Info (EM) for {code} ---")
    df = ak.stock_individual_info_em(symbol=code)
    if not df.empty:
        print(df)
if __name__ == "__main__":
    check_em_individual()
