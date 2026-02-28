import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def check_snapshot_details():
    code = "513980"
    res = ef.stock.get_quote_snapshot(code)
    print("--- Snapshot Detailed Fields ---")
    print(res)

if __name__ == "__main__":
    check_snapshot_details()
