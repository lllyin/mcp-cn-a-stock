"""mcp_app 模块完整性的冒烟检查。

起因：加 trading_calendar 工具时把 ``get_datasource`` 的 import 挤掉——import
不报错（名字只在函数体内引用），一调 kline_daily/kline_range 才 NameError，
整类工具 400。全量测试没抓到是因为没有用例走到那个 handler。这里把模块级
名字的存在性钉住：任何 import 被挤掉，这条先红。
"""

import sys


def test_names_the_tool_handlers_reference_still_resolve():
    module = sys.modules["finmcp.mcp_app"] if "finmcp.mcp_app" in sys.modules else None
    if module is None:
        import finmcp.mcp_app  # noqa: F401
        module = sys.modules["finmcp.mcp_app"]
    # kline_daily / kline_range 的 handler 体内引用这些名字
    for name in ("get_datasource", "research", "report_contract", "market_session"):
        assert hasattr(module, name), f"finmcp.mcp_app 丢了 {name}——有 handler 会在调用时才 NameError"
