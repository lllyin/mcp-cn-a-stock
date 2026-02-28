import efinance as ef

def check_snapshot_format():
    code = "600519"
    res = ef.stock.get_quote_snapshot(code)
    print("Type:", type(res))
    if hasattr(res, 'columns'):
        print("Columns:", res.columns.tolist())
        print("Shape:", res.shape)
    print(res)

if __name__ == "__main__":
    check_snapshot_format()
