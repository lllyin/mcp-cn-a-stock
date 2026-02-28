import pandas as pd
import numpy as np
import efinance as ef
import akshare_proxy_patch
import talib

# Initialize patch
akshare_proxy_patch.install_patch("101.201.173.125", "", 50)

def calculate_china_kdj(df, n=9, m1=3, m2=3):
    """
    Calculate KDJ using the standard recursive formula common in Chinese trading software.
    K = (2/3)*prev_K + (1/3)*current_RSV
    D = (2/3)*prev_D + (1/3)*current_K
    """
    low_min = df['最低'].rolling(window=n).min()
    high_max = df['最高'].rolling(window=n).max()
    rsv = (df['收盘'] - low_min) / (high_max - low_min) * 100
    rsv = rsv.fillna(50) # Standard initialization
    
    k = []
    d = []
    curr_k = 50.0
    curr_d = 50.0
    
    for val in rsv:
        curr_k = (2/3) * curr_k + (1/3) * val
        curr_d = (2/3) * curr_d + (1/3) * curr_k
        k.append(curr_k)
        d.append(curr_d)
        
    k = np.array(k)
    d = np.array(d)
    j = 3 * k - 2 * d
    return k, d, j

def compare_kdj_methods():
    code = "513980"
    # Get enough data for warmup
    df = ef.stock.get_quote_history(code, beg='20251001', end='20260227', fqt=1)
    
    # Method 1: Current TA-Lib SMA Method
    slowk, slowd = talib.STOCH(
        df['最高'].values, df['最低'].values, df['收盘'].values,
        fastk_period=9, slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0
    )
    slowj = 3 * slowk - 2 * slowd
    
    # Method 2: Chinese Standard Recursive Method
    k_cn, d_cn, j_cn = calculate_china_kdj(df)
    
    print(f"Comparison for {code} on 2026-02-27:")
    print(f"Prices: Close: {df.iloc[-1]['收盘']}, High: {df.iloc[-1]['最高']}, Low: {df.iloc[-1]['最低']}")
    print("-" * 50)
    print(f"Current System (TA-Lib SMA): K: {slowk[-1]:.2f}, D: {slowd[-1]:.2f}, J: {slowj[-1]:.2f}")
    print(f"Chinese Software (Recursive): K: {k_cn[-1]:.2f}, D: {d_cn[-1]:.2f}, J: {j_cn[-1]:.2f}")
    print(f"User's App Report:           K: 17.46, D: 25.44, J: 1.50")

if __name__ == "__main__":
    compare_kdj_methods()
