"""基本数据多级回退。

这一层存在的理由：改动前七个维度（总市值、流通市值、市盈率动/静、市净率、换手率、
股票名称）全挂在 ``ef.stock.get_base_info`` 一个调用上，它抛异常时被外层
``except`` 一把接住，连已经取到的 snapshot 数据也一起丢了。
"""

import pytest

from qtf_mcp.datasource import basic_info as bi


class _Provider(bi.BasicInfoProvider):
    """可编程的假 provider：记录被喂了什么，返回预设结果。"""

    def __init__(self, name, result=None, boom=False):
        self.name = name
        self.result = result
        self.boom = boom
        self.calls = []

    def fetch(self, query, symbol):
        self.calls.append((query, symbol))
        if self.boom:
            raise RuntimeError("上游炸了")
        return self.result


def _info(source, **kwargs):
    return bi.BasicInfo(symbol="SH600519", source=source, **kwargs)


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(bi, "_PROVIDERS", {})
    return bi._PROVIDERS


class TestResolve:
    def test_the_first_complete_source_wins(self, clean_registry):
        first = _Provider("a", _info("a", name="茅台", last=1330.0,
                                     total_market_cap=1e12, float_market_cap=1e12))
        second = _Provider("b", _info("b", name="不该用到"))
        bi.register(first)
        bi.register(second)

        info = bi.resolve("600519", "SH600519", order=("a", "b"))
        assert info.source == "a" and info.name == "茅台"
        assert second.calls == []          # 第一个够用就不该问第二个

    def test_a_half_result_is_completed_by_the_next_source(self, clean_registry):
        """东财的 snapshot 只给名称和最新价，市值那一组要靠腾讯补上。

        这是整层的核心：不合并的话，东财"半通"时市值永远补不上——它返回了非 None，
        循环就停了。
        """
        bi.register(_Provider("eastmoney", _info("eastmoney", name="茅台", last=1330.0)))
        bi.register(_Provider("tencent", _info("tencent", name="腾讯给的名", last=1329.0,
                                               total_market_cap=1.66e12,
                                               float_market_cap=1.66e12, pe_ttm=18.67, pb=6.62)))
        info = bi.resolve("600519", "SH600519", order=("eastmoney", "tencent"))

        assert info.source == "eastmoney+tencent"
        # 先配置的源优先：名称和最新价还是东财的
        assert info.name == "茅台" and info.last == 1330.0
        # 缺的字段由后面的源补
        assert info.total_market_cap == 1.66e12 and info.pe_ttm == 18.67 and info.pb == 6.62

    def test_a_source_that_raises_does_not_stop_the_chain(self, clean_registry):
        bi.register(_Provider("bad", boom=True))
        bi.register(_Provider("good", _info("good", name="茅台", last=1.0,
                                            total_market_cap=1.0, float_market_cap=1.0)))
        info = bi.resolve("600519", "SH600519", order=("bad", "good"))
        assert info.source == "good"

    def test_require_valuation_off_stops_at_the_first_answer(self, clean_registry):
        """ETF 和指数本来就不取市值，没必要为它多问一个源。"""
        first = _Provider("a", _info("a", name="半导体ETF", last=0.976))
        second = _Provider("b", _info("b", total_market_cap=1.0))
        bi.register(first)
        bi.register(second)
        info = bi.resolve("512480", "SH512480", order=("a", "b"), require_valuation=False)
        assert info.source == "a"
        assert second.calls == []

    def test_all_sources_failing_returns_none(self, clean_registry):
        bi.register(_Provider("a", None))
        bi.register(_Provider("b", boom=True))
        assert bi.resolve("600519", "SH600519", order=("a", "b")) is None

    def test_both_identifiers_reach_the_provider(self, clean_registry):
        """两个标识必须都传到：一个给按名字查的源，一个给按代码查的源。

        踩过两次——先把六位码喂给东财（拿到深市平安银行），改完又把不带前缀的
        六位码喂给腾讯（还是平安银行）。
        """
        provider = _Provider("a", None)
        bi.register(provider)
        bi.resolve("上证指数", "SH000001", order=("a",))
        assert provider.calls == [("上证指数", "SH000001")]


class TestRegistry:
    def test_duplicate_registration_is_refused(self, clean_registry):
        bi.register(_Provider("a"))
        with pytest.raises(ValueError):
            bi.register(_Provider("a"))
        bi.register(_Provider("a"), replace=True)

    def test_a_nameless_provider_is_refused(self, clean_registry):
        with pytest.raises(ValueError):
            bi.register(_Provider(""))

    def test_the_order_comes_from_the_environment(self, clean_registry, monkeypatch):
        bi.register(_Provider("eastmoney"))
        bi.register(_Provider("tencent"))
        monkeypatch.setenv(bi.PROVIDER_ORDER_ENV, "tencent,eastmoney")
        assert bi.configured_order() == ("tencent", "eastmoney")

    def test_the_layer_can_be_switched_off(self, clean_registry, monkeypatch):
        bi.register(_Provider("eastmoney"))
        monkeypatch.setenv(bi.PROVIDER_ORDER_ENV, "off")
        assert bi.configured_order() == ()

    def test_an_unknown_name_is_skipped_not_fatal(self, clean_registry, monkeypatch):
        bi.register(_Provider("tencent"))
        monkeypatch.setenv(bi.PROVIDER_ORDER_ENV, "nope,tencent")
        assert bi.configured_order() == ("tencent",)


class TestDerivedFields:
    def test_total_shares_comes_from_market_cap_over_price(self):
        """东财本来也是这么派生的，所以换源之后市盈率(静) 的算法完全不变。"""
        info = _info("x", last=1330.0, total_market_cap=1662609e4 * 100)
        assert info.total_shares == pytest.approx(1662609e4 * 100 / 1330.0)

    def test_total_shares_is_none_without_a_price(self):
        assert _info("x", total_market_cap=1e12).total_shares is None

    def test_has_valuation_needs_both_market_caps(self):
        assert not _info("x", name="茅台", last=1.0).has_valuation
        assert not _info("x", total_market_cap=1e12).has_valuation
        assert _info("x", total_market_cap=1e12, float_market_cap=1e12).has_valuation


class TestCleaning:
    def test_nan_is_not_a_name(self):
        """按名字查指数时东财返回一个字段全是 NaN 的 series。

        NaN 是 float，``or`` 判定为真——不挡住就会一路渲染成"股票名称: nan"。
        """
        assert bi._text(float("nan")) is None
        assert bi._text(None) is None
        assert bi._text("  ") is None
        assert bi._text(" 茅台 ") == "茅台"

    def test_placeholders_are_not_numbers(self):
        """腾讯对 ETF 的市盈率给空串、对指数的市净率给 0.00。

        两者都是"这类标的没有这一项"，不是取数失败；当成 None 交出去，让上层按
        缺失处理，而不是渲染出一个 0。
        """
        for raw in ("", "-", "--", None, float("nan"), "0", "0.00", "abc"):
            assert bi._number(raw) is None, raw
        assert bi._number("6.62") == 6.62
        assert bi._number(6.62) == 6.62


class TestTencentProvider:
    def test_the_field_indices_are_the_verified_ones(self):
        """索引是用服务器真数据反查出来的，不是按位置猜的。

        [39] 不是东财的市盈率(动)：600519 上它是 20.42 而东财是 18.67。改这张表
        之前先去看模块 docstring 里那次比对。
        """
        assert bi.TENCENT_FIELDS["float_market_cap_yi"] == 44
        assert bi.TENCENT_FIELDS["total_market_cap_yi"] == 45
        assert bi.TENCENT_FIELDS["pe_ttm"] == 52

    def test_market_to_book_is_deliberately_not_taken(self):
        """市净率在 [46]，但腾讯那个值不如项目里已有的本地回退准。

        以服务器的东财值为真，7 个标的上最大偏差：腾讯 1.87%（宁德时代 4.36 vs
        4.28），现价/每股净资产 0.42%。不取它，留空让渲染层走本地那条。
        """
        assert "pb" not in bi.TENCENT_FIELDS

    def test_market_cap_is_converted_from_yi_to_yuan(self, monkeypatch):
        parts = ["1", "贵州茅台", "600519", "1330.00"] + [""] * 40
        parts += ["16626.09", "16626.09", "6.62"]        # [44] [45] [46]
        parts += [""] * 5 + ["18.67"]                    # [52]
        payload = 'v_sh600519="' + "~".join(parts) + '";'

        class _Response:
            text = payload
            encoding = "gbk"

        monkeypatch.setitem(
            __import__("sys").modules,
            "requests",
            type("M", (), {"get": staticmethod(lambda *a, **k: _Response())}),
        )
        info = bi.TencentBasicInfoProvider().fetch("600519", "SH600519")
        assert info.total_market_cap == pytest.approx(16626.09e8)
        assert info.float_market_cap == pytest.approx(16626.09e8)
        assert info.pe_ttm == 18.67
        assert info.pb is None          # 故意不取，见上一个测试
        assert info.name == "贵州茅台" and info.last == 1330.0

    def test_a_mismatched_code_is_rejected(self, monkeypatch):
        """腾讯对未知代码返回 pv_none_match，不校验代码就会把别人的数据认下来。"""
        parts = ["1", "平安银行", "000001", "11.89"] + [""] * 49
        payload = 'v_sh000001="' + "~".join(parts) + '";'

        class _Response:
            text = payload
            encoding = "gbk"

        monkeypatch.setitem(
            __import__("sys").modules,
            "requests",
            type("M", (), {"get": staticmethod(lambda *a, **k: _Response())}),
        )
        # 请求的是 sh000001（上证指数），返回的代码是 000001 —— 一致，所以放行。
        assert bi.TencentBasicInfoProvider().fetch("上证指数", "SH000001") is not None
        # 请求 sh600519 却返回 000001 的数据，必须拒掉。
        assert bi.TencentBasicInfoProvider().fetch("600519", "SH600519") is None

    def test_a_symbol_without_digits_is_not_queried(self):
        assert bi.TencentBasicInfoProvider().fetch("上证指数", "上证指数") is None


class TestEastmoneyProvider:
    def _install(self, monkeypatch, base_info, snapshot):
        import sys

        def make(value):
            if isinstance(value, Exception):
                def call(_code):
                    raise value
            else:
                def call(_code):
                    return value
            return call

        module = type("M", (), {"stock": type("S", (), {
            "get_base_info": staticmethod(make(base_info)),
            "get_quote_snapshot": staticmethod(make(snapshot)),
        })()})()
        monkeypatch.setitem(sys.modules, "efinance", module)

    def test_a_raising_base_info_does_not_discard_the_snapshot(self, monkeypatch):
        """改动前的行为：base_info 抛异常，外层 except 把 snapshot 也丢了。

        代码里本来写了一条 snapshot 兜底分支，但它的条件是"返回空"，而 base_info
        是抛异常，所以那条分支永远走不到。
        """
        import pandas as pd

        self._install(
            monkeypatch,
            ValueError("Expecting value: line 1 column 1 (char 0)"),
            pd.Series({"名称": "贵州茅台", "最新价": 1330.0}),
        )
        info = bi.EastmoneyBasicInfoProvider().fetch("600519", "SH600519")
        assert info is not None
        assert info.name == "贵州茅台" and info.last == 1330.0
        assert info.total_market_cap is None      # 市值只在 base_info 里，确实没有
        assert not info.has_valuation             # 所以要继续问下一个源

    def test_both_calls_failing_returns_none(self, monkeypatch):
        self._install(monkeypatch, ValueError("x"), ValueError("y"))
        assert bi.EastmoneyBasicInfoProvider().fetch("600519", "SH600519") is None

    def test_an_all_nan_series_is_not_data(self, monkeypatch):
        """按名字查指数时东财返回这种 series：不是空 DataFrame，也不抛异常。"""
        import pandas as pd

        nan = float("nan")
        self._install(
            monkeypatch,
            pd.Series({"股票名称": nan, "总市值": nan, "市净率": nan}),
            None,
        )
        assert bi.EastmoneyBasicInfoProvider().fetch("上证指数", "SH000001") is None

    def test_a_full_series_is_used_as_is(self, monkeypatch):
        import pandas as pd

        self._install(
            monkeypatch,
            pd.Series({"股票名称": "贵州茅台", "总市值": 1.6626e12,
                       "流通市值": 1.6626e12, "市盈率(动)": 18.67, "市净率": 6.62}),
            pd.Series({"名称": "贵州茅台", "最新价": 1330.0}),
        )
        info = bi.EastmoneyBasicInfoProvider().fetch("600519", "SH600519")
        assert info.has_valuation
        assert info.pe_ttm == 18.67 and info.pb == 6.62
        assert info.total_shares == pytest.approx(1.6626e12 / 1330.0)


class TestAdapterQueryCodes:
    """``_fetch_realtime_sync`` 喂给两个源的标识必须各自对路。

    这一层最容易静默失效的地方：efinance 只认不带前缀的六位码（实测
    ``get_quote_snapshot("SH600519")`` 返回全 NaN，``"600519"`` 才是贵州茅台），
    而腾讯必须要带前缀（``000001`` 会被猜成深市的平安银行）。喂错了东财会整条
    失效，而腾讯兜底会把症状盖住——只看输出比对是发现不了的。
    """

    def _capture(self, monkeypatch):
        seen = {}

        def fake_resolve(query, symbol, **kwargs):
            seen.update(query=query, symbol=symbol, kwargs=kwargs)
            return bi.BasicInfo(symbol=symbol, source="fake", name="x", last=1.0)

        from qtf_mcp.datasource import cn_stock_source as css

        monkeypatch.setattr(css.basic_info, "resolve", fake_resolve)
        return seen, css

    def test_a_stock_is_queried_by_bare_code_and_prefixed_symbol(self, monkeypatch):
        seen, css = self._capture(monkeypatch)
        css.CNStockDataSource()._fetch_realtime_sync("600519", "SH600519")
        assert seen["query"] == "600519"        # 东财：不带前缀
        assert seen["symbol"] == "SH600519"     # 腾讯：带前缀
        assert seen["kwargs"]["require_valuation"] is True

    def test_an_index_is_queried_by_name_when_one_is_known(self, monkeypatch):
        import qtf_mcp.symbols

        monkeypatch.setattr(qtf_mcp.symbols, "get_symbol_name", lambda _s: "上证指数")
        seen, css = self._capture(monkeypatch)
        css.CNStockDataSource()._fetch_realtime_sync("000001", "SH000001")
        assert seen["query"] == "上证指数"        # 东财按代码查不到指数
        assert seen["symbol"] == "SH000001"       # 腾讯要带前缀
        assert seen["kwargs"]["require_valuation"] is False   # 指数不取市值

    def test_an_index_without_a_known_name_falls_back_to_the_symbol(self, monkeypatch):
        """现状就是这一支：``get_symbol_name`` 现在对所有标的都返回空字符串
        （启动时 "load markets failed"），所以指数走的是带前缀的 symbol。

        efinance 不认这种写法，于是指数的基本数据必然落到腾讯。这与改动前的取法
        逐字一致——改动前的结果是整个 realtime 判失败，现在是腾讯把名称和最新价
        补上，报告里那三行（代码/名称/日期）不变。
        """
        import qtf_mcp.symbols

        monkeypatch.setattr(qtf_mcp.symbols, "get_symbol_name", lambda _s: "")
        seen, css = self._capture(monkeypatch)
        css.CNStockDataSource()._fetch_realtime_sync("000001", "SH000001")
        assert seen["query"] == "SH000001"
        assert seen["kwargs"]["require_valuation"] is False

    def test_an_etf_does_not_ask_a_second_source_for_valuation(self, monkeypatch):
        seen, css = self._capture(monkeypatch)
        css.CNStockDataSource()._fetch_realtime_sync("512480", "SH512480")
        assert seen["query"] == "512480"
        assert seen["kwargs"]["require_valuation"] is False

    def test_valuation_is_dropped_for_etfs_and_indices(self, monkeypatch):
        """腾讯给得出 ETF 和指数的市值，但报告里本来就没有这几维。

        让它们凭空多出来是新功能不是补缺，要单独决定，别让"修回退"顺手改了输出。
        """
        from qtf_mcp.datasource import cn_stock_source as css

        def rich(query, symbol, **kwargs):
            return bi.BasicInfo(symbol=symbol, source="tencent", name="x", last=2.0,
                                total_market_cap=1e12, float_market_cap=1e12,
                                pe_ttm=17.06, pb=3.0)

        monkeypatch.setattr(css.basic_info, "resolve", rich)
        source = css.CNStockDataSource()
        for code, symbol in (("512480", "SH512480"), ("000001", "SH000001")):
            info = source._fetch_realtime_sync(code, symbol)["info"]
            assert info["总市值"] == 0.0 and info["流通市值"] == 0.0
            assert info["动态市盈率"] == 0.0 and "市净率" not in info
            assert info["股票简称"] == "x" and info["最新价"] == 2.0

        info = source._fetch_realtime_sync("600519", "SH600519")["info"]
        assert info["总市值"] == 1e12 and info["动态市盈率"] == 17.06


def test_the_real_providers_are_registered():
    assert bi.registered() == ("eastmoney", "tencent")
    assert bi.DEFAULT_PROVIDER_ORDER == ("eastmoney", "tencent")
