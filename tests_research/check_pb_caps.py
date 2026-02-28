import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_pb_and_caps():
    code = "603986" # 兆易创新
    print(f"--- Checking PB and Market Caps for {code} ---")
    
    # Check realtime quotes
    df = ef.stock.get_realtime_quotes()
    match = df[df['股票代码'] == code]
    if not match.empty:
        print("\n[Realtime Quote Columns Available]")
        print(df.columns.tolist())
        print("\n[Values]")
        # Check if '市净率' is in columns
        row = match.iloc[0]
        for col in df.columns:
            if '市净率' in col or '市值' in col or '动态' in col:
                print(f"{col}: {row[col]}")

if __name__ == "__main__":
    check_pb_and_caps()
