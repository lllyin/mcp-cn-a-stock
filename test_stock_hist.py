import akshare as ak

def test_stock_zh_a_hist():
    """
    Test the stock_zh_a_hist API.
    Parameters:
    - symbol: stock code
    - period: daily, weekly, monthly
    - start_date: beginning date (YYYYMMDD)
    - end_date: ending date (YYYYMMDD)
    - adjust: "" (no adjust), "qfq" (forward), "hfq" (backward)
    """
    print("Testing stock_zh_a_hist API...")
    try:
        # Using exact parameters from documentation
        stock_zh_a_hist_df = ak.stock_zh_a_hist(
            symbol="600734",
            period="daily",
            start_date="20050501",
            end_date="20050520",
            adjust="hfq"
        )
        print("API Call Successful!")
        print("Data Sample:")
        print(stock_zh_a_hist_df)
    except Exception as e:
        print(f"Error calling API: {e}")

if __name__ == "__main__":
    test_stock_zh_a_hist()
