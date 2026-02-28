import akshare as ak
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_ak_amplitude():
    code = "513980"
    print("--- AkShare fund_etf_hist_em ---")
    try:
        df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date="20260226", end_date="20260227", adjust="qfq")
        print(df[['日期', '最高', '最低', '振幅']])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_ak_amplitude()
