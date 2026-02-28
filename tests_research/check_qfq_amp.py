import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_qfq():
    code = "513980"
    df = ef.stock.get_quote_history(code, beg='20260227', end='20260227', fqt=1)
    if not df.empty:
         print(df[['日期', '收盘', '最高', '最低', '振幅']])

if __name__ == "__main__":
    check_qfq()
