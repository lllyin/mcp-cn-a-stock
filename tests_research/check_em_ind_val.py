import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_em_indicator():
    code = "603986" # 兆易创新
    print(f"--- AkShare stock_financial_analysis_indicator_em for {code} ---")
    try:
        # Actually there's a better one: stock_financial_analysis_indicator_em
        df = ak.stock_financial_analysis_indicator_em(symbol=code)
        if not df.empty:
            target_cols = ['日期', '每股收益', '每股净资产', '净利润', '净资产收益率']
            print(df[target_cols].head(10))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_em_indicator()
