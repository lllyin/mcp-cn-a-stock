import efinance as ef

def check_snapshot_index():
    code = "600519"
    res = ef.stock.get_quote_snapshot(code)
    print("Full Index:", res.index.tolist())
    # Try to see if total market value is in get_realtime_quotes
    print("\n--- realtime quotes ---")
    rt = ef.stock.get_realtime_quotes()
    matches = rt[rt['股票代码'] == code]
    if not matches.empty:
        print("Realtime Columns:", matches.columns.tolist())
        print(matches.iloc[0])

if __name__ == "__main__":
    check_snapshot_index()
