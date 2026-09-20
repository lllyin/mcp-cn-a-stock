"""财务数据多级回退。

这一层存在的理由：财务那一整段（净利润、营收、每股收益、每股净资产、净资产收益率，
以及拿净利润当分母的静态市盈率）原先只挂在 ``ak.stock_financial_abstract_ths`` 一次
HTTP 上，同花顺主机一次 TLS 抖动就整段消失（归档 11 天 1575 次取数失败 6 次）。

这里的用例盯两件事：链的语义（先出数者全份胜出、缺列不算出数），以及新浪适配层
**必须把表交成同花顺那张的形状**——两个映射陷阱都在模块 docstring 里记着，测试把它们
钉死，因为错的方式都是静默的。
"""

import pandas as pd
import pytest

from finmcp.datasource import finance_source as fs


class _Provider(fs.FinanceProvider):
    """可编程的假 provider：记录被喂了什么，返回预设结果。"""

    def __init__(self, name, frame=None, boom=False):
        self.name = name
        self.frame = frame
        self.boom = boom
        self.calls = []

    def fetch(self, code, symbol):
        self.calls.append((code, symbol))
        if self.boom:
            raise RuntimeError("上游炸了")
        return self.frame


def _frame(**overrides):
    """一张"够用"的财务表，形状照同花顺那张（行序从旧到新）。"""
    data = {
        "报告期": ["2025-12-31", "2026-03-31"],
        "净利润": ["55.22亿", "16.35亿"],
        "营业总收入": ["393.53亿", "103.23亿"],
        "基本每股收益": [7.6446, 2.2556],
        "每股净资产": [52.06, 54.21],
        "净资产收益率": ["16.41%", "4.25%"],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def _sina_raw():
    """新浪那张"指标 × 报告期"的宽表——值取自 2026-09-21 实测的 SZ002371。

    列从新到旧（和真表一致），并且故意留着跨组重复的指标名，用来证明只取常用指标。
    """
    rows = [
        ("常用指标", "归母净利润", 3.370021e9, 1.634739e9, 5.521993e9),
        ("常用指标", "净利润", 3.228548e9, 1.567956e9, 5.408907e9),      # 不是要用那列
        ("常用指标", "营业总收入", 2.016142e10, 1.032286e10, 3.935311e10),
        ("常用指标", "基本每股收益", 4.6486, 2.2556, 7.6446),
        ("常用指标", "每股净资产", 55.87084, 54.21165, 52.06324),
        ("常用指标", "净资产收益率(ROE)", 8.58, 4.25, 16.41),
        ("成长能力", "归母净利润", 9.99e9, 9.99e9, 9.99e9),              # 别的组，不能混进来
        ("每股指标", "基本每股收益", 9.99, 9.99, 9.99),
    ]
    return pd.DataFrame(rows, columns=["选项", "指标", "20260630", "20260331", "20251231"])


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(fs, "_PROVIDERS", {})
    return fs._PROVIDERS


class TestResolve:
    def test_the_first_complete_source_wins_and_the_second_is_not_asked(self, clean_registry):
        first, second = _Provider("ths", _frame()), _Provider("sina", _frame())
        fs.register(first)
        fs.register(second)

        outcome = fs.resolve("002371", "SZ002371", order=("ths", "sina"))
        assert outcome.provider == "ths"
        assert second.calls == []

    def test_a_transport_failure_on_the_primary_falls_to_the_next(self, clean_registry):
        """主源挂掉是常态不是异常：整段财务数据不该跟着一次 TLS 抖动一起消失。"""
        fs.register(_Provider("ths", boom=True))
        sina = _Provider("sina", _frame())
        fs.register(sina)

        outcome = fs.resolve("002371", "SZ002371", order=("ths", "sina"))
        assert outcome.provider == "sina"
        assert sina.calls == [("002371", "SZ002371")]

    def test_a_frame_missing_a_column_is_not_a_result(self, clean_registry):
        """半份表不能算出数——下游会把缺的那列静默成 0，比整段没有更难查。"""
        partial = _frame().drop(columns=["净资产收益率"])
        fs.register(_Provider("ths", partial))
        fs.register(_Provider("sina", _frame()))

        outcome = fs.resolve("002371", "SZ002371", order=("ths", "sina"))
        assert outcome.provider == "sina"

    def test_no_source_gives_a_frame_returns_none(self, clean_registry):
        fs.register(_Provider("ths", boom=True))
        fs.register(_Provider("sina", None))
        assert fs.resolve("002371", "SZ002371", order=("ths", "sina")) is None

    def test_an_empty_frame_from_the_primary_does_not_stop_the_chain(self, clean_registry):
        fs.register(_Provider("ths", pd.DataFrame()))
        fs.register(_Provider("sina", _frame()))
        assert fs.resolve("002371", "SZ002371", order=("ths", "sina")).provider == "sina"

    def test_the_frames_do_not_get_merged(self, clean_registry):
        """先出数者全份胜出：两家的报告期轴不同，按列拼会把值配错年份。"""
        fs.register(_Provider("ths", _frame()))
        fs.register(_Provider("sina", _frame(每股净资产=[9.9, 9.9])))
        assert list(fs.resolve("002371", "SZ002371", order=("ths", "sina")).frame["每股净资产"]) == \
            [52.06, 54.21]


class TestConfiguredOrder:
    def test_default_is_primary_then_fallback(self, monkeypatch):
        monkeypatch.setattr(fs, "_PROVIDERS", {"ths": object(), "sina": object()})
        monkeypatch.delenv(fs.PROVIDER_ORDER_ENV, raising=False)
        assert fs.configured_order() == ("ths", "sina")

    def test_the_order_is_configurable(self, monkeypatch):
        """顺序即优先级：换环境时可以让新浪打头，但同一个字段永远由固定顺序决定。"""
        monkeypatch.setattr(fs, "_PROVIDERS", {"ths": object(), "sina": object()})
        monkeypatch.setenv(fs.PROVIDER_ORDER_ENV, "sina,ths")
        assert fs.configured_order() == ("sina", "ths")

    def test_off_disables_the_whole_layer(self, monkeypatch):
        monkeypatch.setattr(fs, "_PROVIDERS", {"ths": object()})
        monkeypatch.setenv(fs.PROVIDER_ORDER_ENV, "off")
        assert fs.configured_order() == ()

    def test_an_unknown_name_is_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setattr(fs, "_PROVIDERS", {"sina": object()})
        monkeypatch.setenv(fs.PROVIDER_ORDER_ENV, "tushare,sina")
        assert fs.configured_order() == ("sina",)

    def test_both_shipped_providers_are_registered(self):
        assert {"ths", "sina"} <= set(fs.registered())


class TestSinaNormalization:
    """适配层的两个静默陷阱。"""

    def test_net_profit_comes_from_the_parent_only_line(self):
        """同花顺的 ``净利润`` 是新浪的 ``归母净利润``。取错一列会静默差 44%。"""
        frame = fs.normalize_sina(_sina_raw())
        # 2026-06-30 归母 33.70亿 对 净利润 32.29亿
        assert frame["净利润"].tolist()[-1].startswith("33.7002")
        assert "32.2854" not in "".join(frame["净利润"])

    def test_other_groups_do_not_leak_in(self):
        assert fs.normalize_sina(_sina_raw())["基本每股收益"].tolist()[-1] == pytest.approx(4.6486)

    def test_a_ratio_arrives_with_its_percent_sign(self):
        """``_parse_numeric_column(is_percent=True)`` 只对**字符串**除以 100。

        交 float 8.58 出去，ROE 会变成 858.00%。
        """
        frame = fs.normalize_sina(_sina_raw())
        assert frame["净资产收益率"].tolist()[-1] == "8.5800%"

    def test_amounts_arrive_with_the_yi_unit(self):
        frame = fs.normalize_sina(_sina_raw())
        assert frame["营业总收入"].tolist()[-1] == "201.614200亿"

    def test_rows_are_oldest_first_like_the_primary_table(self):
        """渲染层用 ``yearly_fin_index`` 从尾部找最新年度期，顺序反了会取到多年前的数。"""
        frame = fs.normalize_sina(_sina_raw())
        assert frame["报告期"].tolist() == ["2025-12-31", "2026-03-31", "2026-06-30"]

    def test_a_missing_cell_reaches_the_parser_as_no_value(self):
        """新浪空着的一格要和同花顺的 ``--`` 表现一致：解析后是 0，不是报错也不是编个数。"""
        from finmcp.datasource.cn_stock_source import CNStockDataSource

        raw = _sina_raw()
        raw.loc[raw["指标"] == "净资产收益率(ROE)", "20260630"] = None
        frame = fs.normalize_sina(raw)
        parsed = CNStockDataSource.__new__(CNStockDataSource)._parse_numeric_column(
            frame["净资产收益率"], is_percent=True)
        assert parsed.tolist() == pytest.approx([0.1641, 0.0425, 0.0])

    def test_no_common_section_returns_none(self):
        raw = _sina_raw()
        raw["选项"] = "盈利能力"
        assert fs.normalize_sina(raw) is None

    def test_empty_or_absent_input_returns_none(self):
        assert fs.normalize_sina(None) is None
        assert fs.normalize_sina(pd.DataFrame()) is None


class TestDropInContract:
    """新浪那张表过完生产解析器之后，必须和同花顺那张逐项相同。

    这是"补一个源"唯一真正要证明的事：不然换源就是换数。数值取自 2026-09-21 实测的
    SZ002371（同花顺那张表本身就是两位小数，所以容差取它的显示精度）。
    """

    def _parse(self, frame):
        from finmcp.datasource.cn_stock_source import CNStockDataSource

        ds = CNStockDataSource.__new__(CNStockDataSource)
        return {
            "净利润": ds._parse_numeric_column(frame["净利润"]),
            "营业总收入": ds._parse_numeric_column(frame["营业总收入"]),
            "基本每股收益": ds._parse_numeric_column(frame["基本每股收益"]),
            "每股净资产": ds._parse_numeric_column(frame["每股净资产"]),
            "净资产收益率": ds._parse_numeric_column(frame["净资产收益率"], is_percent=True),
        }

    def test_parsed_values_match_the_primary_table(self):
        parsed = self._parse(fs.normalize_sina(_sina_raw()))
        assert parsed["净利润"].tolist() == pytest.approx([55.22e8, 16.35e8, 33.70e8], rel=1e-3)
        assert parsed["营业总收入"].tolist() == pytest.approx([393.53e8, 103.23e8, 201.61e8], rel=1e-3)
        assert parsed["基本每股收益"].tolist() == pytest.approx([7.6446, 2.2556, 4.6486])
        assert parsed["每股净资产"].tolist() == pytest.approx([52.06, 54.21, 55.87], abs=0.005)
        assert parsed["净资产收益率"].tolist() == pytest.approx([0.1641, 0.0425, 0.0858], abs=5e-5)


class TestWiring:
    """判据不能只活在测试里——调用方必须真的走这条链。"""

    def test_the_datasource_asks_the_chain_not_akshare_directly(self):
        import inspect

        from finmcp.datasource import cn_stock_source

        src = inspect.getsource(cn_stock_source.CNStockDataSource._fetch_finance_sync)
        assert "finance_source.resolve" in src
        assert "stock_financial_abstract_ths" not in src, "又写回单源了"

    def test_the_provider_that_answered_is_carried_onto_the_data(self):
        """来路要一路传到渲染层：换源的那一份报告会和前一天差一截口径。"""
        import inspect

        from finmcp.datasource import base, cn_stock_source

        assert 'finance_data.get("provider")' in inspect.getsource(cn_stock_source)
        assert '"SOURCE": self.finance_provider' in inspect.getsource(base)
        assert "PRIMARY_PROVIDER" in inspect.getsource(
            __import__("finmcp.research", fromlist=["research"]).build_financial_data)

    def _render(self, source):
        from io import StringIO

        import numpy as np

        from finmcp.research import build_financial_data

        def ns(year, month, day):
            import datetime

            return int(datetime.datetime(year, month, day).timestamp() * 1e9)

        dataset = ({"DATE": np.array([ns(2024, 12, 31), ns(2025, 12, 31)], dtype=np.int64),
                    "ROE": np.array([0.1641, 0.0858]), "MR": np.array([1e10, 2e10]),
                    "NP": np.array([1e9, 2e9]), "EPS": np.array([1.0, 2.0]),
                    "NAVPS": np.array([10.0, 11.0]), "SOURCE": source}, "1q")
        fp = StringIO()
        build_financial_data(fp, "SZ002371", {"SYMBOL": "SZ002371", "_DS_FINANCE": dataset})
        return fp.getvalue()

    def test_a_fallback_report_says_where_its_finance_data_came_from(self):
        section = self._render("sina")
        assert "- 本节财务数据来自新浪" in section
        assert "年度净资产收益率两家的口径不同" in section

    @pytest.mark.parametrize("source", ["ths", ""])
    def test_the_primary_table_prints_no_such_note(self, source):
        """正常的报告不许为一次没发生的回退喊话。"""
        assert "本节财务数据来自" not in self._render(source)
