import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_tail_finance():
    code = "603986" # 兆易创新
    print(f"--- Tail Finance for {code} ---")
    df_fin = ak.stock_financial_abstract_ths(symbol=code, indicator="主要指标")
    if not df_fin.empty:
        print(df_fin.tail())
if __name__ == "__main__":
    check_tail_finance()
