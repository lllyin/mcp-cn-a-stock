import efinance as ef
import akshare_proxy_patch

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def debug_turnover():
    code = "603986" # 兆易创新
    print(f"--- Debugging Turnover for {code} ---")
    
    # Base info
    info = ef.stock.get_base_info(code)
    print("\n[Base Info]")
    print(info[['股票名称', '总市值', '流通市值']])
    
    # Snapshot for latest price and volume
    snapshot = ef.stock.get_quote_snapshot(code)
    print("\n[Snapshot]")
    print(snapshot[['时间', '最新价', '成交量', '成交额']])
    
    latest_price = snapshot['最新价']
    volume_lots = snapshot['成交量']
    total_market_val = info['总市值']
    float_market_val = info['流通市值']
    
    total_shares = total_market_val / latest_price
    float_shares = float_market_val / latest_price
    
    print(f"\nCalculated Total Shares: {total_shares/1e8:.2f}亿")
    print(f"Calculated Float Shares: {float_shares/1e8:.2f}亿")
    print(f"Volume (Lots): {volume_lots}")
    
    # Turnover calculation
    turnover_total = (volume_lots * 100) / total_shares
    turnover_float = (volume_lots * 100) / float_shares
    
    print(f"\nTurnover (Total Shares): {turnover_total:.2%}")
    print(f"Turnover (Float Shares): {turnover_float:.2%}")

if __name__ == "__main__":
    debug_turnover()
