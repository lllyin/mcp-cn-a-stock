import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_valuation_fields():
    code = "603986"
    print(f"--- Checking Valuation Fields for {code} ---")
    
    # Check base info
    info = ef.stock.get_base_info(code)
    print("\n[Base Info Columns]")
    print(info.index.tolist())
    print("\n[Base Info Values]")
    print(info)
    
    # Check realtime quotes (contains PE/PB usually)
    rt = ef.stock.get_realtime_quotes()
    match = rt[rt['股票代码'] == code]
    if not match.empty:
        print("\n[Realtime Quote Columns]")
        print(match.columns.tolist())
        print("\n[Realtime Quote Values]")
        print(match.iloc[0])

if __name__ == "__main__":
    check_valuation_fields()
