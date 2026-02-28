import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_navps():
    code = "603986" # 兆易创新
    df_fin = ak.stock_financial_abstract_ths(symbol=code, indicator="主要指标")
    if not df_fin.empty:
        # Columns in ths: 报告期, 每股净资产 (元), etc
        match_navps = [c for c in df_fin.columns if "每股净资产" in c]
        print(df_fin[['报告期'] + match_navps].tail())
if __name__ == "__main__":
    check_navps()
