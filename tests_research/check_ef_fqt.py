import efinance as ef

def check_adjust_mapping():
    code = "600519"
    print("--- Checking efinance fqt mapping ---")
    
    # fqt: 0-no, 1-qfq, 2-hfq
    for f in [0, 1, 2]:
        df = ef.stock.get_quote_history(code, beg='20240102', end='20240102', fqt=f)
        if not df.empty:
            print(f"fqt={f}: Close={df.iloc[0]['收盘']}")

if __name__ == "__main__":
    check_adjust_mapping()
