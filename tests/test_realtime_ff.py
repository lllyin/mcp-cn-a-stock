"""
Realtime fund-flow page routing tests.
"""

from qtf_mcp.datasource.realtime_ff import get_fund_flow_display_name, get_fund_flow_url


def test_core_indices_use_specific_index_pages():
    assert get_fund_flow_url("000001") == "https://data.eastmoney.com/zjlx/zs000001.html"
    assert get_fund_flow_url("SH000001") == "https://data.eastmoney.com/zjlx/zs000001.html"
    assert get_fund_flow_url("399001") == "https://data.eastmoney.com/zjlx/zs399001.html"
    assert get_fund_flow_url("SZ399001") == "https://data.eastmoney.com/zjlx/zs399001.html"
    assert get_fund_flow_url("399006") == "https://data.eastmoney.com/zjlx/zs399006.html"
    assert get_fund_flow_url("SZ399006") == "https://data.eastmoney.com/zjlx/zs399006.html"


def test_indices_without_realtime_pages_return_none():
    assert get_fund_flow_url("000688") is None
    assert get_fund_flow_url("SH000688") is None
    assert get_fund_flow_url("dpzjlx") == "https://data.eastmoney.com/zjlx/dpzjlx.html"


def test_stock_uses_stock_page():
    assert get_fund_flow_url("300308") == "https://data.eastmoney.com/zjlx/300308.html"


def test_core_indices_use_stable_display_names():
    assert get_fund_flow_display_name("000001", "沪深资金流向") == "上证指数"
    assert get_fund_flow_display_name("399001", "沪深资金流向") == "深证成指"
    assert get_fund_flow_display_name("399006", "沪深资金流向") == "创业板指"
    assert get_fund_flow_display_name("300308", "中际旭创") == "中际旭创"
