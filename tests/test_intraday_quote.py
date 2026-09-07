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

        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        result = upsert_intraday_bar(self._frame(), self._quote())

        assert len(result) == 2
        row = result.iloc[-1]
        assert row["日期"] == datetime.date(2026, 9, 4)
        assert row["收盘"] == pytest.approx(110.23)
        assert row["开盘"] == pytest.approx(113.51)
        assert row["成交量"] == pytest.approx(171026.0)   # 手，与历史行同单位

    def test_derives_change_from_the_previous_bar(self):
        """涨跌以表里最后一根的收盘为前收，保证序列连续。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        row = upsert_intraday_bar(self._frame(close=110.91), self._quote()).iloc[-1]

        assert row["涨跌额"] == pytest.approx(110.23 - 110.91)
        assert row["涨跌幅"] == pytest.approx((110.23 / 110.91 - 1) * 100)
        assert row["振幅"] == pytest.approx((115.32 - 110.04) / 110.91 * 100)

    def test_does_not_append_when_the_quote_is_not_newer(self):
        """休市时行情的日期就是最后一根的日期，不该重复追加。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        quote = self._quote(as_of="20260903150000")
        assert len(upsert_intraday_bar(self._frame(), quote)) == 1

    def test_requires_a_full_bar(self):
        """页头行情没有开高低，拼不出 bar 就不要拼。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        partial = self._quote(open=None, high=None, low=None)
        assert len(upsert_intraday_bar(self._frame(), partial)) == 1

    def test_requires_a_timestamp(self):
        """没有自报日期就无法判断新旧，宁可不补——不去猜本地时钟。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        assert len(upsert_intraday_bar(self._frame(), self._quote(as_of=None))) == 1

    def test_does_not_append_past_the_requested_end_date(self):
        """历史查询不能被实时行情污染。

        date=2026-08-27 会拿到截到 08-27 的序列，若再接一根今天的，报告的
        "数据日期"就变成今天，5/20/60 日窗口也跟着漂——生产归档比对时就是这样
        暴露出来的。
        """
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        frame = self._frame(last_date="2026-08-27")
        assert len(upsert_intraday_bar(frame, self._quote(), not_after="2026-08-27")) == 1
        # 不指定日期时 load_raw_data 传的是"明天"，当天这根要能通过。
        assert len(upsert_intraday_bar(frame, self._quote(), not_after="2026-09-05")) == 2

    def test_end_date_accepts_dates_and_datetimes(self):
        import datetime

        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        frame = self._frame()
        for limit in (
            datetime.date(2026, 9, 3),
            datetime.datetime(2026, 9, 3, 15, 0),
            "2026-09-03",
        ):
            assert len(upsert_intraday_bar(frame, self._quote(), not_after=limit)) == 1
        # 解析不了的值不该悄悄挡掉当天这根
        assert len(upsert_intraday_bar(frame, self._quote(), not_after="不是日期")) == 2

    def test_skips_backward_adjusted_series(self):
        """后复权的最新价被缩放过，接一根原始价上去是错的。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        result = upsert_intraday_bar(self._frame(), self._quote(), adjust="hfq")
        assert len(result) == 1

    def test_no_quote_leaves_the_frame_untouched(self):
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        frame = self._frame()
        assert upsert_intraday_bar(frame, None) is frame


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


class TestTodaysBarOverridesTheDailyEndpoint:
    """当天那一根只认实时端点，日线端点给的当天行一律不作准。

    2026-09-07 定位到的问题：同花顺日线年份文件带当天那一行，但它是收盘前的盘中
    快照、收盘后不回填。收盘后约两小时按分钟采样，27 个「有当天那行」的样本里
    没有一个等于定稿值——SH000001 停在 3931.85 / 4.20亿手，定稿是 3932.70 /
    4.77亿手。而它在指数取数顺序里排第一，于是那个未定稿值直接进了报告的当日
    收盘、涨跌幅、成交量，以及每一条含当日的均线/均量。

    修法不动取数顺序（同花顺的历史成交量是最准的一家），只换当天那一根。
    """

    TODAY = __import__("datetime").date(2026, 9, 7)

    def _frame(self, rows):
        import pandas as pd

        from finmcp.datasource.cn_stock_source import FALLBACK_FRAME_COLUMNS

        base = {"开盘": 3942.51, "最高": 3948.42, "最低": 3916.49,
                "成交额": 7.998e11, "振幅": 0.81, "涨跌幅": 0.04,
                "涨跌额": 1.73, "换手率": 0.88}
        return pd.DataFrame(
            [{**base, "日期": d, "收盘": c, "成交量": v} for d, c, v in rows],
            columns=FALLBACK_FRAME_COLUMNS,
        )

    def _quote(self, **kw):
        base = dict(symbol="SH000001", source="tencent", last=3932.70,
                    prev_close=3930.12, open=3942.51, high=3948.42, low=3916.49,
                    volume_lots=477375261.0, amount_yuan=8.979e11,
                    turnover_pct=0.88, as_of="20260907161402")
        base.update(kw)
        return iq.IntradayQuote(**base)

    def _both_days(self):
        import datetime

        return [(datetime.date(2026, 9, 4), 3930.12, 537286160.0),
                (datetime.date(2026, 9, 7), 3931.85, 420313370.0)]   # ← 未定稿

    def test_a_stale_same_day_row_is_replaced_not_kept(self):
        """核心那一条：源给了当天，但给的是盘中快照，要被实时值顶掉。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        result = upsert_intraday_bar(self._frame(self._both_days()), self._quote(),
                                     today=self.TODAY)
        assert len(result) == 2, "是覆盖，不是又追加一根"
        row = result.iloc[-1]
        assert row["收盘"] == 3932.70
        assert row["成交量"] == 477375261.0
        assert row["日期"] == self.TODAY

    def test_the_derived_columns_come_off_the_bar_before_today(self):
        """覆盖时前收盘必须取倒数第二根，取最后一根就是拿今天算今天，涨跌幅恒为 0。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        row = upsert_intraday_bar(self._frame(self._both_days()), self._quote(),
                                  today=self.TODAY).iloc[-1]
        assert row["涨跌幅"] == pytest.approx((3932.70 / 3930.12 - 1) * 100)
        assert row["涨跌额"] == pytest.approx(3932.70 - 3930.12)
        assert row["振幅"] == pytest.approx((3948.42 - 3916.49) / 3930.12 * 100)

    def test_history_is_untouched(self):
        """只换当天那一根——同花顺的历史成交量是最准的一家，不能被顺手改掉。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        result = upsert_intraday_bar(self._frame(self._both_days()), self._quote(),
                                     today=self.TODAY)
        assert result.iloc[0]["收盘"] == 3930.12
        assert result.iloc[0]["成交量"] == 537286160.0

    def test_a_settled_earlier_day_is_never_replaced(self):
        """周末查：行情自报的还是周五，而周五那一行**已经定稿**。

        覆盖它只有坏处——会把创业板指那一行正确的成交量换成腾讯低 3.89% 的口径，
        而且一覆盖就是整个周末。
        """
        import datetime

        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        friday = [(datetime.date(2026, 9, 3), 3900.0, 5.0e8),
                  (datetime.date(2026, 9, 4), 3930.12, 537286160.0)]
        quote = self._quote(last=3930.12, volume_lots=1.0, as_of="20260904150000")
        result = upsert_intraday_bar(self._frame(friday), quote,
                                     today=datetime.date(2026, 9, 5))
        assert result.iloc[-1]["成交量"] == 537286160.0, "定稿行不许被实时快照覆盖"
        assert len(result) == 2

    def test_a_lone_same_day_row_is_left_alone(self):
        """只有一行时算不出涨跌幅，宁可不动——kline_daily 只请求一天就是这种。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        one = [(self.TODAY, 3931.85, 420313370.0)]
        result = upsert_intraday_bar(self._frame(one), self._quote(), today=self.TODAY)
        assert result.iloc[-1]["收盘"] == 3931.85
        assert len(result) == 1

    def test_a_future_row_is_left_alone(self):
        """表里有比行情更新的一天：不该发生，真发生了也不动它。"""
        import datetime

        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        ahead = [(self.TODAY, 3931.85, 4.2e8),
                 (datetime.date(2026, 9, 8), 3999.0, 4.0e8)]
        result = upsert_intraday_bar(self._frame(ahead), self._quote(), today=self.TODAY)
        assert result.iloc[-1]["收盘"] == 3999.0
        assert len(result) == 2

    def test_turnover_survives_a_quote_that_does_not_report_it(self):
        """行情没给换手率时保留源里那个值，别用 0 抹掉它。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        row = upsert_intraday_bar(self._frame(self._both_days()),
                                  self._quote(turnover_pct=None),
                                  today=self.TODAY).iloc[-1]
        assert row["换手率"] == 0.88

    def test_a_pinned_historical_query_is_still_not_polluted(self):
        """not_after 那道闸门不能被新逻辑绕过。"""
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        result = upsert_intraday_bar(self._frame(self._both_days()), self._quote(),
                                     not_after="2026-09-04", today=self.TODAY)
        assert result.iloc[-1]["收盘"] == 3931.85, "钉了日期就不该被今天的行情动"

    def test_hfq_is_still_refused(self):
        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        result = upsert_intraday_bar(self._frame(self._both_days()), self._quote(),
                                     adjust="hfq", today=self.TODAY)
        assert result.iloc[-1]["收盘"] == 3931.85

    def test_the_override_is_visible_in_the_log(self, caplog):
        """这条日志是判断上游当日行有多不靠谱的唯一信号，不能只在 DEBUG 里。"""
        import logging

        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        with caplog.at_level(logging.INFO, logger="finmcp"):
            upsert_intraday_bar(self._frame(self._both_days()), self._quote(),
                                today=self.TODAY)
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert any("未定稿" in m for m in messages), messages
        assert any("3931.85" in m and "3932.7" in m for m in messages), messages

    def test_an_already_settled_same_day_row_does_not_shout(self, caplog):
        """源的当日行已经等于实时值时，覆盖是无害的，但不该刷 INFO 日志。"""
        import logging

        from finmcp.datasource.cn_stock_source import upsert_intraday_bar

        rows = self._both_days()
        rows[-1] = (self.TODAY, 3932.70, 477375261.0)
        with caplog.at_level(logging.INFO, logger="finmcp"):
            upsert_intraday_bar(self._frame(rows), self._quote(), today=self.TODAY)
        assert not [r for r in caplog.records if r.levelno >= logging.INFO]


class TestVolumeUnitNormalisation:
    """``volume_lots`` 的单位是手，而上游的单位**在同一个端点内部都不统一**。

    2026-09-07 定位：腾讯 qt.gtimg.cn 的 ``[6]`` 对主板/创业板是手，对科创板
    （688xxx）是股，而 provider 直接把它当手用——报告里 SH688981 的当日成交量是
    3131.48 万手，真实 31.2 万手，整整 100 倍，而且和同一份报告的成交额 38.74亿
    自相矛盾（3131万手 × 124元 = 3887亿）。

    判定用 ``成交额/收盘`` 反算股数，不用代码前缀名单——名单会漏，漏一个就是 100 倍。
    """

    def test_a_star_market_stock_reported_in_shares_is_converted(self):
        """SH688981 实测：raw 31,314,788 是股，成交额反算 31,212,053 股。"""
        got = iq.to_lots(31_314_788, amount_yuan=3.874040e9, last=124.12,
                         symbol="SH688981", source="tencent")
        assert got == pytest.approx(313_147.88)

    def test_a_main_board_stock_reported_in_lots_is_left_alone(self):
        """SH600519 实测：raw 25,250 已经是手。"""
        got = iq.to_lots(25_250, amount_yuan=3.336030e9, last=1316.01,
                         symbol="SH600519", source="tencent")
        assert got == 25_250

    def test_the_tonghuashun_caliber_is_always_shares(self):
        """同花顺 realhead 的 [13] 一律是股，同一条推断也能判对。"""
        got = iq.to_lots(2_524_962, amount_yuan=3.336030e9, last=1316.01,
                         symbol="SH600519", source="tonghuashun")
        assert got == pytest.approx(25_249.62)

    def test_it_gives_up_rather_than_guess(self, caplog):
        """没有成交额就定不了单位。宁可这一维缺，也不要把 100 倍的数写进报告。"""
        import logging

        with caplog.at_level(logging.WARNING, logger="finmcp"):
            assert iq.to_lots(12345, amount_yuan=None, last=10.0,
                              symbol="SZ000001", source="tencent") is None
        assert any("无法定单位" in r.getMessage() for r in caplog.records)

    def test_zero_volume_is_not_a_unit_problem(self):
        """停牌日成交量是 0，别把它当成定不了单位。"""
        assert iq.to_lots(0, amount_yuan=0, last=10.0,
                          symbol="SZ000001", source="tencent") == 0

    def test_an_odd_magnitude_still_returns_a_value_but_shouts(self, caplog):
        """判据落在两簇之间时要留告警——这一维承载全部兜底成交量，不能静默。"""
        import logging

        # implied_shares = 1e8/10 = 1e7；raw 取 5e5 → ratio 0.05，正好落在
        # "手"（0.01）和"股"（1.0）两簇之间，既判不准也不能静默。
        with caplog.at_level(logging.WARNING, logger="finmcp"):
            iq.to_lots(500_000, amount_yuan=1.0e8, last=10.0,
                       symbol="SZ000001", source="tencent")
        assert any("量级异常" in r.getMessage() for r in caplog.records)

    def test_the_tencent_provider_normalises_a_star_market_stock(self, monkeypatch):
        """整条链：科创板标的过一遍 provider，出来必须是手。"""
        import requests

        parts = ["v_sh688981", "中芯国际", "688981", "124.12", "121.14", "122.50",
                 "31314788"] + [""] * 23 + ["20260907153000"] + [""] * 6 + \
                ["387404.0", "1.57"] + [""] * 2 + ["125.30", "122.22"] + [""] * 20

        class FakeResponse:
            text = "~".join(parts)
            encoding = "gbk"

        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
        quote = iq.TencentQuoteProvider().fetch("SH688981", iq.QuoteContext())
        assert quote.volume_lots == pytest.approx(313_147.88), \
            "科创板的 [6] 是股，必须换成手，否则报告里是 100 倍"

    def test_an_index_skips_the_inference(self, monkeypatch):
        """指数的"收盘"是点位，成交额/点位 算不出股数，只能按端点口径原样用。"""
        import requests

        parts = ["v_sh000001", "上证指数", "000001", "3932.70", "3930.12", "3942.51",
                 "477375261"] + [""] * 23 + ["20260907161402"] + [""] * 6 + \
                ["89790401", "0.98"] + [""] * 2 + ["3948.42", "3916.49"] + [""] * 20

        class FakeResponse:
            text = "~".join(parts)
            encoding = "gbk"

        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
        quote = iq.TencentQuoteProvider().fetch("SH000001", iq.QuoteContext())
        assert quote.volume_lots == 477_375_261, "指数原样用，别去除 100"


class TestTonghuashunQuoteProvider:
    """当日那一根的第二个来源。

    ``upsert_intraday_bar`` 之后当天那一根完全依赖这一层，腾讯就成了单点——它一
    失败当天那根就拼不出来。这个源不同域不同厂，实测收盘价与腾讯 12/12 逐位一致。
    """

    #: 字段号取自实测（上证指数 2026-09-07 收盘）：10 最新 6 昨收 7 开 8 高 9 低
    #: 13 成交量(股) 19 成交额 5 代码 updateTime 数据时刻。
    PAYLOAD = (
        'quotebridge_v6_realhead_hs_1A0001_last({"items":{'
        '"10":"3932.70","6":"3930.12","7":"3942.51","8":"3948.42","9":"3916.49",'
        '"13":"47737526000.00","19":"897904010000.00","199112":"0.07",'
        '"1968584":"1.000","1771976":"0.884","5":"1A0001","name":"上证指数",'
        '"time":"2026-09-07 17:33:38 北京时间","updateTime":"2026-09-07 15:00"}})'
    )

    def _fetch(self, monkeypatch, symbol="SH000001", payload=None, status=200):
        import requests

        body = self.PAYLOAD if payload is None else payload

        class FakeResponse:
            status_code = status
            text = body

        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
        return iq.TonghuashunQuoteProvider().fetch(symbol, iq.QuoteContext())

    def test_it_parses_the_numeric_fields(self, monkeypatch):
        quote = self._fetch(monkeypatch)
        assert quote.source == "tonghuashun"
        assert quote.last == pytest.approx(3932.70)
        assert quote.prev_close == pytest.approx(3930.12)
        assert quote.open == pytest.approx(3942.51)
        assert quote.high == pytest.approx(3948.42)
        assert quote.low == pytest.approx(3916.49)
        assert quote.amount_yuan == pytest.approx(8.9790401e11)
        assert quote.has_ohlc is True

    def test_the_volume_is_shares_even_for_an_index(self, monkeypatch):
        """[13] 一律是股，指数也是：47,737,526,000 股 = 4.77亿手，与交易所定稿一致。"""
        quote = self._fetch(monkeypatch)
        assert quote.volume_lots == pytest.approx(477_375_260)

    def test_the_turnover_field_is_1968584_not_1771976(self, monkeypatch):
        """两个字段都是小数、量级也像，取错那个会让"这个源没有换手率"的结论成立。

        判据是拿腾讯的换手率逐个比：1968584 五个标的全中（茅台 0.202/0.20、
        50ETF 8.137/8.14），1771976 五个全不中（茅台 0.906 vs 0.20）。
        """
        quote = self._fetch(monkeypatch)
        assert quote.turnover_pct == pytest.approx(1.000)      # 1968584
        assert quote.turnover_pct != pytest.approx(0.884)      # 1771976，取错就是这个

    def test_the_data_timestamp_is_compacted(self, monkeypatch):
        """契约要求 as_of 的前 8 位能按 %Y%m%d 解析，分隔符必须去掉。

        而且取的是 updateTime（数据时刻）不是 time（服务器时刻）。
        """
        assert self._fetch(monkeypatch).as_of == "20260907150000"

    def test_a_mismatched_code_is_refused(self, monkeypatch):
        """踩过：hs_000001 返回的是平安银行。拿回来不是这只票就得判失败。"""
        payload = self.PAYLOAD.replace('"5":"1A0001"', '"5":"000001"')
        assert self._fetch(monkeypatch, payload=payload) is None

    def test_a_non_200_is_a_failure(self, monkeypatch):
        assert self._fetch(monkeypatch, status=502) is None

    def test_a_body_that_is_not_jsonp_is_a_failure(self, monkeypatch):
        assert self._fetch(monkeypatch, payload="<html>maintenance</html>") is None

    def test_an_unmappable_symbol_is_skipped_without_a_request(self):
        """沪市 000 开头不在内部码表里时不发请求——问了会拿回同名深市个股。"""
        assert iq.TonghuashunQuoteProvider().fetch("SH000998", iq.QuoteContext()) is None

    def test_indices_prefer_it_and_everything_else_prefers_tencent(self):
        """按类别分，不按标的。

        指数用它：创业板指当日成交量四方核对——东财 172,310,434、同花顺
        172,310,430、腾讯/新浪 165,865,859（低 3.885%），东财是基准源。
        个股不用它：12 个标的实测两家逐位一致，而同花顺的限流策略未知、个股是
        流量大头，没有收益就不该把热路径换过去。
        """
        assert "tonghuashun" in iq.registered()
        for index_symbol in ("SH000001", "SZ399006", "SH000688", "BJ899050"):
            order = iq.configured_order(index_symbol)
            assert order.index("tonghuashun") < order.index("tencent"), index_symbol
        for other in ("SH600519", "SZ300750", "SH688981", "SH512480", "BJ920021"):
            order = iq.configured_order(other)
            assert order.index("tencent") < order.index("tonghuashun"), other

    def test_the_index_order_is_configurable_on_its_own(self, monkeypatch):
        monkeypatch.setenv("INTRADAY_QUOTE_PROVIDERS_INDEX", "tencent")
        assert iq.configured_order("SZ399006") == ("tencent",)
        assert "tonghuashun" in iq.configured_order("SH600519")
