import efinance as ef
import pandas as pd

def check_efinance_capabilities():
    code = "600519" # Maotai
    etf_code = "513980"
    
    print("--- 1. K-line (Stock & ETF) ---")
    print(f"Maotai: {not ef.stock.get_quote_history(code).empty}")
    print(f"ETF: {not ef.stock.get_quote_history(etf_code).empty}")
    
    print("\n--- 2. Base Info / Real-time ---")
    info = ef.stock.get_base_info(code)
    print("Base Info Columns:", info.index.tolist())
    
    print("\n--- 3. Fund Flow (History) ---")
    flow = ef.stock.get_history_bill(code)
    if not flow.empty:
        print("Fund Flow Columns:", flow.columns.tolist())
    
    print("\n--- 4. Financial Data ---")
    # efinance doesn't seem to have a single "financial abstract" like THS
    # Let's check what's available
    try:
        # efinance has some financial functions, let's list them or try a common one
        # Based on documentation search (mental): get_financial_summary? No.
        # Let's check the dir(ef.stock)
        print("ef.stock functions:", [f for f in dir(ef.stock) if not f.startswith('_')])
    except Exception as e:
        print(f"Error checking functions: {e}")

if __name__ == "__main__":
    check_efinance_capabilities()
