import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_all_rt_columns():
    df = ef.stock.get_realtime_quotes()
    print("All Columns:", df.columns.tolist())
    # See if PE/PB are there
    target = "603986"
    match = df[df['股票代码'] == target]
    if not match.empty:
        print(match.iloc[0])

if __name__ == "__main__":
    check_all_rt_columns()
