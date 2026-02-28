import efinance as ef
import akshare_proxy_patch
import talib
import numpy as np

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def compute_kdj(close, high, low, n=9, m1=3, m2=3):
    slowk, slowd = talib.STOCH(
        high, low, close,
        fastk_period=n,
        slowk_period=m1,
        slowk_matype=0,
        slowd_period=m2,
        slowd_matype=0
    )
    j = 3 * slowk - 2 * slowd
    return slowk, slowd, j

def analyze_sh513980_accuracy():
    code = "513980"
    print(f"--- Analyzing Accuracy for {code} ---")
    
    # Get sufficient data for TA calculation (at least 100 days to avoid warmup issues)
    df = ef.stock.get_quote_history(code, beg='20250101', end='20260227', fqt=1)
    
    if df.empty:
        print("Error: No data retrieved.")
        return

    # Prepare data for talib
    close = df['收盘'].values.astype(float)
    high = df['最高'].values.astype(float)
    low = df['最低'].values.astype(float)
    dates = df['日期'].values
    
    # Compute indicators
    k, d, j = compute_kdj(close, high, low)
    rsi6 = talib.RSI(close, timeperiod=6)
    
    # Look at the last 10 days
    print("\nDate       | Close | High  | Low   | K     | D     | J     | RSI(6)")
    print("-" * 75)
    for i in range(-10, 0):
        print(f"{dates[i]} | {close[i]:.3f} | {high[i]:.3f} | {low[i]:.3f} | {k[i]:.2f} | {d[i]:.2f} | {j[i]:.2f} | {rsi6[i]:.2f}")

    # Check for duplicates or missing dates
    diff = np.diff(pd.to_datetime(df['日期']))
    # Note: weekends cause > 1 day gaps, but many weekdays missing?
    
if __name__ == "__main__":
    import pandas as pd
    analyze_sh513980_accuracy()
