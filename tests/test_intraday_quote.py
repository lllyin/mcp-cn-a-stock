"""盘中行情的多级回退。

不联网：腾讯的响应用真实抓下来的报文做样本，页面来源用 fixture 里的真实页面。
"""

import logging
from pathlib import Path

import pytest

from finmcp.datasource import intraday_quote as iq
from finmcp.datasource.fund_flow_page import parse_fund_flow_page

FULL_PAGE = Path(__file__).parent / "fixtures" / "eastmoney_zjlx_full_300408.html"

# 2026-09-04 盘中从 qt.gtimg.cn 抓下的真实报文（截断到用得着的字段之后）。
TENCENT_PAYLOAD = (
    'v_sz300408="51~三环集团~300408~110.23~110.91~113.51~171026~85000~86026~110.23'
    "~1~110.22~2~110.21~3~110.20~4~110.19~5~110.24~1~110.25~2~110.26~3~110.27~4"
    '~110.28~5~~20260904103000~-0.68~-0.61~115.32~110.04~110.23/171026/1924150000'
    '~171026~192415~0.91~66.84~~115.32~110.04~4.76~2201.75~2201.75~9.74~121.99'
    '~99.82~0.92~395~110.90~"'
)


@pytest.fixture(scope="module")
def page_context():
    page = parse_fund_flow_page(FULL_PAGE.read_text(encoding="utf-8"))
    return iq.QuoteContext(fund_flow_page=page)


class TestRegistry:
    """插件式：注册、抽拔、组合都不该动调用方。"""

    def test_builtin_providers_are_registered(self):
        assert "fund_flow_page" in iq.registered()
        assert "tencent" in iq.registered()

    def test_order_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "tencent,fund_flow_page")
        assert iq.configured_order() == ("tencent", "fund_flow_page")

    def test_a_source_can_be_dropped_by_configuration(self, monkeypatch):
        monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "tencent")
        assert iq.configured_order() == ("tencent",)

    def test_the_whole_layer_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "off")
        assert iq.configured_order() == ()
        assert iq.resolve("SZ300408") is None

    def test_unknown_names_are_skipped_not_fatal(self, monkeypatch):
        """配置里写错一个名字不该让服务起不来。"""
        monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "nope,tencent")
        assert iq.configured_order() == ("tencent",)

    def test_a_custom_provider_can_be_plugged_in(self, monkeypatch):
        class Fake(iq.QuoteProvider):
            name = "fake"

            def fetch(self, symbol, context):
                return iq.IntradayQuote(symbol=symbol, source=self.name, last=1.0)

        iq.register(Fake())
        try:
            monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "fake")
            quote = iq.resolve("SZ300408")
            assert quote is not None and quote.source == "fake"
        finally:
            iq.unregister("fake")

        assert "fake" not in iq.registered()

    def test_duplicate_registration_is_refused(self):
        with pytest.raises(ValueError, match="已存在"):
            iq.register(iq.TencentQuoteProvider())


class TestFundFlowPageProvider:
    def test_reads_the_header_quote_without_a_request(self, page_context):
        """fixture 页头：最新价 110.91 涨跌 3.04 换手 2.26% 总手 42.29万手 金额 47.14亿。"""
        quote = iq.FundFlowPageQuoteProvider().fetch("SZ300408", page_context)

        assert quote.source == "fund_flow_page"
        assert quote.last == pytest.approx(110.91)
        assert quote.prev_close == pytest.approx(107.87)     # 最新 - 涨跌
        assert quote.volume_lots == pytest.approx(422900.0)  # 42.29万手
        assert quote.amount_yuan == pytest.approx(4.714e9)   # 47.14亿
        assert quote.turnover_pct == pytest.approx(2.26)

    def test_cannot_build_a_bar(self, page_context):
        """页头没有开高低，所以不能拿它拼 K 线。"""
        quote = iq.FundFlowPageQuoteProvider().fetch("SZ300408", page_context)

        assert quote.has_ohlc is False
        assert quote.open is None

    def test_returns_none_without_a_page(self):
        assert iq.FundFlowPageQuoteProvider().fetch("SZ300408", iq.QuoteContext()) is None


class TestTencentProvider:
    def _provider(self, monkeypatch, payload=TENCENT_PAYLOAD):
        class FakeResponse:
            text = payload
            encoding = "gbk"

        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
        return iq.TencentQuoteProvider()

    def test_parses_every_field(self, monkeypatch):
        quote = self._provider(monkeypatch).fetch("SZ300408", iq.QuoteContext())

        assert quote.last == pytest.approx(110.23)
        assert quote.prev_close == pytest.approx(110.91)
        assert quote.open == pytest.approx(113.51)
        assert quote.high == pytest.approx(115.32)
        assert quote.low == pytest.approx(110.04)
        assert quote.volume_lots == pytest.approx(171026.0)
        assert quote.amount_yuan == pytest.approx(1.92415e9)  # 192415万
        assert quote.turnover_pct == pytest.approx(0.91)
        assert quote.as_of == "20260904103000"

    def test_can_build_a_bar(self, monkeypatch):
        quote = self._provider(monkeypatch).fetch("SZ300408", iq.QuoteContext())

        assert quote.has_ohlc is True
        assert quote.change == pytest.approx(-0.68)
        assert quote.change_pct == pytest.approx(-0.613, abs=1e-3)

    def test_rejects_a_response_for_another_symbol(self, monkeypatch):
        """腾讯对未知代码会返回 pv_none_match，别把它当成数据。"""
        provider = self._provider(monkeypatch, 'v_pv_none_match="1";')
        assert provider.fetch("SZ300408", iq.QuoteContext()) is None

    def test_code_normalisation(self):
        assert iq.tencent_code("SZ300408") == "sz300408"
        assert iq.tencent_code("SH600547") == "sh600547"
        assert iq.tencent_code("300408") == "sz300408"
        assert iq.tencent_code("600547") == "sh600547"
        assert iq.tencent_code("430047") == "bj430047"
        assert iq.tencent_code("abc") is None


class TestResolveAndCrossCheck:
    def test_prefers_the_zero_cost_source(self, monkeypatch, page_context):
        """页面已经加载过时不该再发请求。"""
        monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "fund_flow_page,tencent")

        import requests

        def forbidden(*a, **k):
            raise AssertionError("已有页面时不应发起行情请求")

        monkeypatch.setattr(requests, "get", forbidden)

        assert iq.resolve("SZ300408", page_context).source == "fund_flow_page"

    def test_falls_through_when_a_full_bar_is_required(self, monkeypatch, page_context):
        """页头拼不出 bar，就该继续往下走而不是返回半个结果。"""
        monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "fund_flow_page,tencent")

        class FakeResponse:
            text = TENCENT_PAYLOAD
            encoding = "gbk"

        import requests

        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())

        quote = iq.resolve("SZ300408", page_context, require_ohlc=True)
        assert quote.source == "tencent"
        assert quote.has_ohlc is True

    def test_a_failing_source_does_not_stop_the_chain(self, monkeypatch, page_context):
        class Broken(iq.QuoteProvider):
            name = "broken"

            def fetch(self, symbol, context):
                raise RuntimeError("boom")

        iq.register(Broken())
        try:
            monkeypatch.setenv(iq.PROVIDER_ORDER_ENV, "broken,fund_flow_page")
            assert iq.resolve("SZ300408", page_context).source == "fund_flow_page"
        finally:
            iq.unregister("broken")

    def test_compare_reports_only_real_divergence(self):
        base = iq.IntradayQuote(symbol="SZ300408", source="a", last=110.0, open=109.0)
        close = iq.IntradayQuote(symbol="SZ300408", source="b", last=110.5, open=109.0)
        far = iq.IntradayQuote(symbol="SZ300408", source="c", last=130.0, open=109.0)

        assert iq.compare(base, close) == []          # 0.45%，在容差内
        assert len(iq.compare(base, far)) == 1
        assert "last" in iq.compare(base, far)[0]

    def test_compare_skips_fields_one_side_lacks(self):
        """页头没有开高低，不该因此报成"不一致"。"""
        page_like = iq.IntradayQuote(symbol="x", source="page", last=110.0)
        full = iq.IntradayQuote(symbol="x", source="tencent", last=110.0, open=113.0)

        assert iq.compare(page_like, full) == []


class TestAppendIntradayBar:
    """把当天这根 bar 补到兜底源的日 K 上。

    腾讯/新浪的日 K 盘中不含当天（实测 2026-09-04 盘中最后一行仍是 09-03），
    东财的含。所以东财一失败，"当日"就退回昨天，而同一份报告里的市值是今天的。
    """

    def _frame(self, last_date="2026-09-03", close=110.91):
        import datetime

        import pandas as pd

        from finmcp.datasource.cn_stock_source import FALLBACK_FRAME_COLUMNS

        return pd.DataFrame(
            [{
                "日期": datetime.date.fromisoformat(last_date),
                "开盘": 110.02, "收盘": close, "最高": 113.95, "最低": 109.09,
                "成交量": 422922.0, "成交额": 4.714e9,
                "振幅": 4.51, "涨跌幅": 2.82, "涨跌额": 3.04, "换手率": 2.26,
            }],
            columns=FALLBACK_FRAME_COLUMNS,
        )

    def _quote(self, **overrides):
        base = dict(
            symbol="SZ300408", source="tencent", last=110.23, prev_close=110.91,
            open=113.51, high=115.32, low=110.04, volume_lots=171026.0,
            amount_yuan=1.92415e9, turnover_pct=0.91, as_of="20260904103000",
        )
        base.update(overrides)
        return iq.IntradayQuote(**base)

    def test_appends_todays_bar(self):
        import datetime

        from finmcp.datasource.cn_stock_source import append_intraday_bar

        result = append_intraday_bar(self._frame(), self._quote())

        assert len(result) == 2
        row = result.iloc[-1]
        assert row["日期"] == datetime.date(2026, 9, 4)
        assert row["收盘"] == pytest.approx(110.23)
        assert row["开盘"] == pytest.approx(113.51)
        assert row["成交量"] == pytest.approx(171026.0)   # 手，与历史行同单位

    def test_derives_change_from_the_previous_bar(self):
        """涨跌以表里最后一根的收盘为前收，保证序列连续。"""
        from finmcp.datasource.cn_stock_source import append_intraday_bar

        row = append_intraday_bar(self._frame(close=110.91), self._quote()).iloc[-1]

        assert row["涨跌额"] == pytest.approx(110.23 - 110.91)
        assert row["涨跌幅"] == pytest.approx((110.23 / 110.91 - 1) * 100)
        assert row["振幅"] == pytest.approx((115.32 - 110.04) / 110.91 * 100)

    def test_does_not_append_when_the_quote_is_not_newer(self):
        """休市时行情的日期就是最后一根的日期，不该重复追加。"""
        from finmcp.datasource.cn_stock_source import append_intraday_bar

        quote = self._quote(as_of="20260903150000")
        assert len(append_intraday_bar(self._frame(), quote)) == 1

    def test_requires_a_full_bar(self):
        """页头行情没有开高低，拼不出 bar 就不要拼。"""
        from finmcp.datasource.cn_stock_source import append_intraday_bar

        partial = self._quote(open=None, high=None, low=None)
        assert len(append_intraday_bar(self._frame(), partial)) == 1

    def test_requires_a_timestamp(self):
        """没有自报日期就无法判断新旧，宁可不补——不去猜本地时钟。"""
        from finmcp.datasource.cn_stock_source import append_intraday_bar

        assert len(append_intraday_bar(self._frame(), self._quote(as_of=None))) == 1

    def test_does_not_append_past_the_requested_end_date(self):
        """历史查询不能被实时行情污染。

        date=2026-08-27 会拿到截到 08-27 的序列，若再接一根今天的，报告的
        "数据日期"就变成今天，5/20/60 日窗口也跟着漂——生产归档比对时就是这样
        暴露出来的。
        """
        from finmcp.datasource.cn_stock_source import append_intraday_bar

        frame = self._frame(last_date="2026-08-27")
        assert len(append_intraday_bar(frame, self._quote(), not_after="2026-08-27")) == 1
        # 不指定日期时 load_raw_data 传的是"明天"，当天这根要能通过。
        assert len(append_intraday_bar(frame, self._quote(), not_after="2026-09-05")) == 2

    def test_end_date_accepts_dates_and_datetimes(self):
        import datetime

        from finmcp.datasource.cn_stock_source import append_intraday_bar

        frame = self._frame()
        for limit in (
            datetime.date(2026, 9, 3),
            datetime.datetime(2026, 9, 3, 15, 0),
            "2026-09-03",
        ):
            assert len(append_intraday_bar(frame, self._quote(), not_after=limit)) == 1
        # 解析不了的值不该悄悄挡掉当天这根
        assert len(append_intraday_bar(frame, self._quote(), not_after="不是日期")) == 2

    def test_skips_backward_adjusted_series(self):
        """后复权的最新价被缩放过，接一根原始价上去是错的。"""
        from finmcp.datasource.cn_stock_source import append_intraday_bar

        result = append_intraday_bar(self._frame(), self._quote(), adjust="hfq")
        assert len(result) == 1

    def test_no_quote_leaves_the_frame_untouched(self):
        from finmcp.datasource.cn_stock_source import append_intraday_bar

        frame = self._frame()
        assert append_intraday_bar(frame, None) is frame


# --- 跨源交叉校验 -------------------------------------------------------------
#
# collect() 和 compare() 这两个零件早就有了却没人调，等于白造。接进 resolve()
# 之后，源之间的口径差会在日志里当场暴露——创业板指成交量差 3.5% 那件事，
# 是靠人工三方比对花了几个钟头才定位的。


class _FixedQuote(iq.QuoteProvider):
    def __init__(self, name, **fields):
        self.name = name
        self._quote = iq.IntradayQuote(symbol="SH600000", source=name, **fields)
        self.calls = 0

    def fetch(self, symbol, context):
        self.calls += 1
        return self._quote


@pytest.fixture
def two_disagreeing_sources(monkeypatch):
    saved = dict(iq._PROVIDERS)
    iq._PROVIDERS.clear()
    a = _FixedQuote("a", last=10.0, open=10.0, high=10.0, low=10.0, volume_lots=100.0)
    b = _FixedQuote("b", last=10.0, open=10.0, high=10.0, low=10.0, volume_lots=200.0)
    iq.register(a)
    iq.register(b)
    monkeypatch.setenv("INTRADAY_QUOTE_PROVIDERS", "a,b")
    yield a, b
    iq._PROVIDERS.clear()
    iq._PROVIDERS.update(saved)


def test_cross_check_is_off_by_default(two_disagreeing_sources, monkeypatch, caplog):
    """默认关：正常路径上问到第一个就停，不为诊断多付一次上游请求。"""
    a, b = two_disagreeing_sources
    monkeypatch.setattr(iq, "INTRADAY_QUOTE_CROSS_CHECK_PCT", 0.0)

    with caplog.at_level(logging.WARNING, logger="finmcp"):
        assert iq.resolve("SH600000").source == "a"

    assert b.calls == 0
    assert "跨源不一致" not in caplog.text


def test_cross_check_reports_a_disagreement(two_disagreeing_sources, monkeypatch, caplog):
    a, b = two_disagreeing_sources
    monkeypatch.setattr(iq, "INTRADAY_QUOTE_CROSS_CHECK_PCT", 1.0)

    with caplog.at_level(logging.WARNING, logger="finmcp"):
        quote = iq.resolve("SH600000")

    # 采用的仍然是第一个源，校验只是观察，不改选择。
    assert quote.source == "a" and quote.volume_lots == 100.0
    assert b.calls == 1
    assert "跨源不一致" in caplog.text and "volume_lots" in caplog.text


def test_cross_check_stays_quiet_when_sources_agree(monkeypatch, caplog):
    saved = dict(iq._PROVIDERS)
    iq._PROVIDERS.clear()
    for name in ("a", "b"):
        iq.register(_FixedQuote(name, last=10.0, volume_lots=100.0))
    monkeypatch.setenv("INTRADAY_QUOTE_PROVIDERS", "a,b")
    monkeypatch.setattr(iq, "INTRADAY_QUOTE_CROSS_CHECK_PCT", 1.0)
    try:
        with caplog.at_level(logging.WARNING, logger="finmcp"):
            iq.resolve("SH600000")
        assert "跨源不一致" not in caplog.text
    finally:
        iq._PROVIDERS.clear()
        iq._PROVIDERS.update(saved)


def test_a_broken_cross_check_never_takes_down_the_fetch(two_disagreeing_sources, monkeypatch):
    """校验是观察点，它自己炸了也不能影响取数。"""
    a, b = two_disagreeing_sources
    monkeypatch.setattr(iq, "INTRADAY_QUOTE_CROSS_CHECK_PCT", 1.0)
    monkeypatch.setattr(b, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    assert iq.resolve("SH600000").source == "a"
