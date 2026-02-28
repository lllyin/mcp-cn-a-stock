import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_25th():
    code = "513980"
    df = ef.stock.get_quote_history(code, beg='20260225', end='20260227')
    print(df[['日期', '收盘', '涨跌幅']])

if __name__ == "__main__":
    check_25th()
