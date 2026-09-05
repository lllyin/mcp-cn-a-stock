"""
研究分析模块单元测试
"""

import datetime
from io import StringIO

import numpy as np
import pytest

from finmcp.research import (
    build_basic_data,
    build_financial_data,
    build_trading_data,
    build_historical_fund_flow_data,
    compute_kdj,
    compute_macd,
    est_fin_ratio,
    filter_sector,
    get_realtime_fund_flow_prefix,
    get_realtime_fund_flow_target,
    has_realtime_fund_flow_values,
    has_today_fund_flow_from_api,
    is_stock,
    print_api_fund_flow_if_today,
    yearly_fin_index,
)


@pytest.mark.asyncio
async def test_trading_data_includes_open_price(sample_stock_data_dict):
    """交易数据使用已有 K 线数据展示当日开盘价。"""
    data = dict(sample_stock_data_dict)
    data["IS_HISTORICAL_QUERY"] = True
    fp = StringIO()

    await build_trading_data(fp, "SZ000001", data)

    expected = f"开盘: {data['OPEN'][-1]:.3f}"
    assert expected in fp.getvalue()


class TestIsStock:
    """测试 is_stock 函数"""

    def test_shanghai_main_board(self):
        """上海主板股票"""
        assert is_stock("SH600000") is True
        assert is_stock("SH601398") is True

    def test_shenzhen_main_board(self):
        """深圳主板股票"""
        assert is_stock("SZ000001") is True
        assert is_stock("SZ000858") is True

    def test_shenzhen_gem(self):
        """创业板股票"""
        assert is_stock("SZ300001") is True
        assert is_stock("SZ300750") is True

    def test_index(self):
        """指数代码"""
        assert is_stock("SH000001") is False
        assert is_stock("SZ399001") is False

    def test_etf(self):
        """ETF代码"""
        assert is_stock("SH510050") is False
        assert is_stock("SZ159919") is False


class TestFilterSector:
    """测试 filter_sector 函数"""

    def test_filter_keywords(self):
        """过滤包含关键词的板块"""
        sectors = ["银行", "MSCI中国", "标普500", "金融"]
        result = filter_sector(sectors)
        assert "银行" in result
        assert "金融" in result
        assert "MSCI中国" not in result
        assert "标普500" not in result

    def test_filter_hugangtong(self):
        """过滤沪股通"""
        sectors = ["科技", "沪股通", "创新药"]
        result = filter_sector(sectors)
        assert "沪股通" not in result
        assert "科技" in result
        assert "创新药" in result

    def test_filter_margin_trading(self):
        """过滤融资融券"""
        sectors = ["新能源", "融资融券", "光伏"]
        result = filter_sector(sectors)
        assert "融资融券" not in result
        assert "新能源" in result

    def test_filter_tonghuashun(self):
        """过滤同花顺相关"""
        sectors = ["同花顺概念", "银行"]
        result = filter_sector(sectors)
        assert len(result) == 1
        assert "银行" in result

    def test_empty_list(self):
        """空列表"""
        assert filter_sector([]) == []

    def test_all_filtered(self):
        """全部被过滤"""
        sectors = ["MSCI中国", "融资融券", "同花顺指数"]
        result = filter_sector(sectors)
        assert result == []


class TestEstFinRatio:
    """测试 est_fin_ratio 函数"""

    def test_q4_report(self):
        """第四季度报告（年报）"""
        date = datetime.datetime(2024, 12, 31)
        assert est_fin_ratio(date) == 1

    def test_q3_report(self):
        """第三季度报告"""
        date = datetime.datetime(2024, 9, 30)
        assert est_fin_ratio(date) == 0.75

    def test_q2_report(self):
        """第二季度报告（半年报）"""
        date = datetime.datetime(2024, 6, 30)
        assert est_fin_ratio(date) == 0.5

    def test_q1_report(self):
        """第一季度报告"""
        date = datetime.datetime(2024, 3, 31)
        assert est_fin_ratio(date) == 0.25

    def test_other_month(self):
        """非季末月份"""
        date = datetime.datetime(2024, 5, 15)
        assert est_fin_ratio(date) == 0

    def test_january(self):
        """1月"""
        date = datetime.datetime(2024, 1, 15)
        assert est_fin_ratio(date) == 0


class TestYearlyFinIndex:
    """测试 yearly_fin_index 函数"""

    def _make_dates(self, date_strs):
        """将日期字符串列表转换为纳秒时间戳数组"""
        timestamps = []
        for d in date_strs:
            dt = datetime.datetime.strptime(d, "%Y-%m-%d")
            timestamps.append(int(dt.timestamp() * 1e9))
        return np.array(timestamps, dtype=np.int64)

    def test_find_last_december(self):
        """找到最后一个12月"""
        dates = self._make_dates(
            [
                "2022-12-31",
                "2023-03-31",
                "2023-06-30",
                "2023-09-30",
                "2023-12-31",
                "2024-03-31",
            ]
        )
        assert yearly_fin_index(dates) == 4

    def test_multiple_decembers(self):
        """多个12月，返回最后一个"""
        dates = self._make_dates(
            [
                "2021-12-31",
                "2022-12-31",
                "2023-12-31",
            ]
        )
        assert yearly_fin_index(dates) == 2

    def test_only_december(self):
        """只有一个12月"""
        dates = self._make_dates(["2023-12-31"])
        assert yearly_fin_index(dates) == 0

    def test_no_december(self):
        """没有12月"""
        dates = self._make_dates(["2024-03-31", "2024-06-30", "2024-09-30"])
        assert yearly_fin_index(dates) == -1

    def test_empty_array(self):
        """空数组"""
        dates = np.array([], dtype=np.int64)
        assert yearly_fin_index(dates) == -1


class TestHistoricalFundFlow:
    """测试历史资金流向表格"""

    def _date_ns(self, value: str) -> int:
        dt = datetime.datetime.strptime(value, "%Y-%m-%d")
        return int(dt.timestamp() * 1e9)

    def test_build_historical_fund_flow_table(self):
        data = {
            "_DS_FUND_FLOW": {
                "DATE": np.array(
                    [
                        self._date_ns("2026-06-01"),
                        self._date_ns("2026-06-02"),
                    ],
                    dtype=np.int64,
                ),
                "CLOSE": np.array([15.53, 15.49], dtype=np.float64),
                "PCT_CHG": np.array([-0.0208, -0.0026], dtype=np.float64),
                "A_A": np.array([-52399200.0, -153000000.0], dtype=np.float64),
                "A_R": np.array([-0.0271, -0.1045], dtype=np.float64),
                "XL_A": np.array([-111000000.0, -116000000.0], dtype=np.float64),
                "XL_R": np.array([-0.0572, -0.0795], dtype=np.float64),
                "L_A": np.array([58401900.0, -36656100.0], dtype=np.float64),
                "L_R": np.array([0.0302, -0.0251], dtype=np.float64),
                "M_A": np.array([4474600.0, 22523400.0], dtype=np.float64),
                "M_R": np.array([0.0023, 0.0154], dtype=np.float64),
                "S_A": np.array([47924700.0, 130000000.0], dtype=np.float64),
                "S_R": np.array([0.0247, 0.0891], dtype=np.float64),
            }
        }
        fp = StringIO()

        build_historical_fund_flow_data(fp, data)

        output = fp.getvalue()
        assert "## 历史资金流向" in output
        assert "| 日期 | 收盘价 | 涨跌幅 | 主力净流入 | 主力占比 |" in output
        assert "| 2026-06-02 | 15.49 | -0.26% | -1.53亿 | -10.45% |" in output
        assert "| 2026-06-01 | 15.53 | -2.08% | -5239.92万 | -2.71% |" in output

    def test_build_historical_fund_flow_respects_query_date(self):
        data = {
            "QUERY_DATE": "2026-06-01",
            "_DS_FUND_FLOW": {
                "DATE": np.array(
                    [
                        self._date_ns("2026-06-01"),
                        self._date_ns("2026-06-02"),
                    ],
                    dtype=np.int64,
                ),
                "CLOSE": np.array([15.53, 15.49], dtype=np.float64),
                "PCT_CHG": np.array([-0.0208, -0.0026], dtype=np.float64),
            },
        }
        fp = StringIO()

        build_historical_fund_flow_data(fp, data)

        output = fp.getvalue()
        assert "2026-06-01" in output
        assert "2026-06-02" not in output


class TestRealtimeFundFlowTarget:
    """测试实时资金流向抓取目标"""

    def test_market_indices_use_specific_index_code(self):
        data = {"IS_MARKET": True}

        assert get_realtime_fund_flow_target("SH000001", data) == "000001"
        assert get_realtime_fund_flow_target("SZ399001", data) == "399001"
        assert get_realtime_fund_flow_target("SZ399006", data) == "399006"

    def test_indices_without_realtime_page_return_none(self):
        assert get_realtime_fund_flow_target("SH000688", {"IS_MARKET": False}) is None
        assert get_realtime_fund_flow_target("SH000688", {"IS_MARKET": True}) is None

    def test_stock_uses_plain_code(self):
        assert get_realtime_fund_flow_target("SZ300308", {"IS_MARKET": False}) == "300308"

    def test_a_single_index_carries_no_scope_prefix(self):
        """前缀只标"是谁的钱"。时间标在段标题上，不再逐行写"今日"。"""
        assert get_realtime_fund_flow_prefix("000001", {"IS_MARKET": True}) == ""
        assert get_realtime_fund_flow_prefix("399001", {"IS_MARKET": True}) == ""
        assert get_realtime_fund_flow_prefix("399006", {"IS_MARKET": True}) == ""

    def test_market_page_prefix_keeps_market_label(self):
        assert get_realtime_fund_flow_prefix("dpzjlx", {"IS_MARKET": True}) == "沪深两市"


class TestRealtimeFundFlowFallback:
    """测试实时资金流向 API fallback"""

    @staticmethod
    def _date_ns(date_str: str) -> int:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return int(dt.timestamp() * 1e9)

    def test_api_fallback_requires_latest_row_to_be_today(self):
        data = {
            "_DS_FUND_FLOW": {
                "DATE": np.array([self._date_ns("2026-07-05")], dtype=np.int64),
            }
        }

        assert has_today_fund_flow_from_api(data, datetime.date(2026, 7, 6)) is False

        data["_DS_FUND_FLOW"]["DATE"] = np.array(
            [self._date_ns("2026-07-06")], dtype=np.int64
        )
        assert has_today_fund_flow_from_api(data, datetime.date(2026, 7, 6)) is True

    def test_api_fallback_prints_existing_fund_flow_fields(self):
        data = {
            "_DS_FUND_FLOW": {
                "DATE": np.array([self._date_ns("2026-07-06")], dtype=np.int64),
            },
            "A_A": np.array([123456789.0], dtype=np.float64),
            "A_R": np.array([0.1234], dtype=np.float64),
        }
        fp = StringIO()

        printed = print_api_fund_flow_if_today(fp, data, datetime.date(2026, 7, 6))

        assert printed is True
        assert "主力净流入: 1.23亿" in fp.getvalue()
        assert "今日" not in fp.getvalue()
        assert "主力净占比: 12.34%" in fp.getvalue()

    def test_realtime_zero_values_are_treated_as_empty(self):
        assert has_realtime_fund_flow_values({"主力净流入": "0.00万"}) is False
        assert has_realtime_fund_flow_values({"主力净流入": "1.23亿"}) is True


class TestComputeKDJ:
    """测试 compute_kdj 函数"""

    def test_basic_calculation(self):
        """基本计算测试"""
        # 创建简单的价格数据
        close = np.array(
            [10, 11, 12, 11, 13, 14, 13, 15, 16, 15], dtype=np.float64
        )
        high = np.array(
            [10.5, 11.5, 12.5, 11.5, 13.5, 14.5, 13.5, 15.5, 16.5, 15.5],
            dtype=np.float64,
        )
        low = np.array(
            [9.5, 10.5, 11.5, 10.5, 12.5, 13.5, 12.5, 14.5, 15.5, 14.5],
            dtype=np.float64,
        )

        k, d, j = compute_kdj(close, high, low)

        # 验证返回值形状
        assert len(k) == len(close)
        assert len(d) == len(close)
        assert len(j) == len(close)

        # 验证 J = 3*K - 2*D
        np.testing.assert_array_almost_equal(j, 3 * k - 2 * d)

    def test_kdj_range(self):
        """KD 值应该在 0-100 范围内（大部分情况）"""
        np.random.seed(42)
        close = np.cumsum(np.random.randn(100)) + 100
        close = close.astype(np.float64)
        high = (close + np.abs(np.random.randn(100))).astype(np.float64)
        low = (close - np.abs(np.random.randn(100))).astype(np.float64)

        k, d, j = compute_kdj(close, high, low)

        # 跳过 NaN 值
        valid_k = k[~np.isnan(k)]
        valid_d = d[~np.isnan(d)]

        assert valid_k.min() >= 0
        assert valid_k.max() <= 100
        assert valid_d.min() >= 0
        assert valid_d.max() <= 100

    def test_custom_parameters(self):
        """自定义参数"""
        close = np.arange(20, dtype=np.float64) + 100
        high = close + 1
        low = close - 1

        k, d, j = compute_kdj(close, high, low, n=5, m1=2, m2=2)

        assert len(k) == 20
        assert len(d) == 20
        assert len(j) == 20


class TestComputeMACD:
    """测试 compute_macd 函数"""

    def test_basic_calculation(self):
        """基本计算测试"""
        close = np.array(
            [10, 11, 12, 11, 13, 14, 13, 15, 16, 15] * 5, dtype=np.float64
        )

        dif, dea = compute_macd(close)

        # 验证返回值形状
        assert len(dif) == len(close)
        assert len(dea) == len(close)

    def test_custom_periods(self):
        """自定义周期参数"""
        close = np.arange(100, dtype=np.float64) + 100

        dif, dea = compute_macd(close, fast=5, slow=10, signal=3)

        assert len(dif) == len(close)
        assert len(dea) == len(close)

    def test_uptrend(self):
        """上升趋势中 DIF 应该为正"""
        close = np.arange(100, dtype=np.float64) + 10

        dif, dea = compute_macd(close)

        # 在上升趋势的后半段，DIF 应该为正
        valid_dif = dif[~np.isnan(dif)]
        assert valid_dif[-1] > 0

    def test_downtrend(self):
        """下降趋势中 DIF 应该为负"""
        close = (100 - np.arange(100)).astype(np.float64)

        dif, dea = compute_macd(close)

        # 在下降趋势的后半段，DIF 应该为负
        valid_dif = dif[~np.isnan(dif)]
        assert valid_dif[-1] < 0


def _ns(year: int, month: int, day: int) -> int:
    return int(datetime.datetime(year, month, day).timestamp() * 1e9)


def _finance_dataset() -> tuple:
    """两个年度报告期，够 build_financial_data 输出一行。"""
    return (
        {
            "DATE": np.array([_ns(2024, 12, 31), _ns(2025, 12, 31)]),
            "MR": np.array([73.75e8, 90.07e8]),
            "NP": np.array([21.90e8, 26.18e8]),
            "EPS": np.array([1.14, 1.37]),
            "NAVPS": np.array([10.37, 11.30]),
            "ROE": np.array([0.1150, 0.1262]),
        },
        "1q",
    )


class TestPriceToBook:
    """市净率取值优先级。

    数值取自 2026-09-03 的 SZ300408：数据源口径 9.78 = 总市值 2214.56 亿 /
    归母净资产 226.44 亿；回退口径 9.38 = 110.910 / 每股净资产 11.82。两者相差
    的正好是最新总股本 19.97 亿股与报告期末股本 19.16 亿股之比。
    """

    def _data(self, **overrides) -> dict:
        data = {
            "SYMBOL": "SZ300408",
            "NAME": "三环集团",
            "DATE": np.array([_ns(2026, 9, 3)]),
            "CLOSE2": np.array([110.910]),
            "TCAP": np.array([19.9672e8]),
            "NAVPS": np.array([11.82]),
            "_DS_FINANCE": _finance_dataset(),
        }
        data.update(overrides)
        return data

    def _pb_line(self, data: dict) -> str:
        fp = StringIO()
        build_basic_data(fp, "SZ300408", data)
        lines = [line for line in fp.getvalue().splitlines() if "市净率" in line]
        return lines[0] if lines else ""

    def test_source_value_wins(self):
        assert self._pb_line(self._data(PB=np.array([9.78]))) == "- 市净率: 9.78"

    def test_falls_back_when_the_realtime_source_failed(self):
        """实时行情失败时 PB 为 0，仍要出数而不是丢字段。"""
        assert self._pb_line(self._data(PB=np.array([0.0]))) == "- 市净率: 9.38"

    def test_falls_back_when_the_source_omits_the_field(self):
        assert self._pb_line(self._data()) == "- 市净率: 9.38"

    def test_omitted_when_neither_is_available(self):
        data = self._data(PB=np.array([0.0]), NAVPS=np.array([0.0]))
        assert self._pb_line(data) == ""


class TestFinancialSectionSymbolCorrection:
    """交易所前缀写错时不能静默丢掉财务数据段。

    2026-09-03 查 SH300408（三环集团实为深市）时，数据源把代码纠正成 SZ300408，
    基本数据段用的是纠正后的值，但 build_financial_data 用的还是入参，
    is_stock("SH300408") 为假，整段财务数据无声消失。
    """

    def _has_section(self, passed_symbol: str, resolved_symbol: str) -> bool:
        fp = StringIO()
        build_financial_data(
            fp,
            passed_symbol,
            {"SYMBOL": resolved_symbol, "_DS_FINANCE": _finance_dataset()},
        )
        return "# 财务数据" in fp.getvalue()

    def test_wrong_prefix_still_renders(self):
        assert self._has_section("SH300408", "SZ300408") is True

    def test_correct_prefix_unchanged(self):
        assert self._has_section("SZ300408", "SZ300408") is True

    def test_index_still_has_no_financial_section(self):
        assert self._has_section("SH000001", "SH000001") is False


class TestEmptyRealtimeFundFlowIsNotZero:
    """浏览器抓到页面但十档全是占位符时，不能渲染成一栏 0。

    2026-09-04 10:19 的实测：历史表 120 行、今日一栏全 0。原来的分支在
    "浏览器没值 且 接口也没有今日数据" 时会落回去打印占位符，等于声称今日
    主力净流入为零，而实际是没拿到。
    """

    def _data(self):
        return {
            "SYMBOL": "SZ300408",
            "DATE": np.array([_ns(2026, 9, 4)]),
            "CLOSE": np.array([111.08]),
            "CLOSE2": np.array([111.08]),
            "OPEN": np.array([113.51]),
            "HIGH": np.array([115.32]),
            "LOW": np.array([110.80]),
            "VOLUME": np.array([119881.0]),
            "AMOUNT": np.array([1.355e9]),
        }

    async def _render(self, monkeypatch, browser_result):
        import json as json_module

        import finmcp.research as research

        async def fake_get_fund_flow(codes, **kwargs):
            return json_module.dumps({codes[0]: browser_result}, ensure_ascii=False)

        monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
        monkeypatch.setattr(
            research, "is_realtime_fund_flow_window", lambda now=None: True
        )
        fp = StringIO()
        await research.build_trading_data(fp, "SZ300408", self._data())
        return fp.getvalue()

    @pytest.mark.asyncio
    async def test_placeholder_amounts_report_unavailable(self, monkeypatch):
        empty = {
            "标的名称": "三环集团(300408)",
            "主力净流入": "0", "主力净比(%)": 0.0,
            "超大单净流入": "0", "超大单净比(%)": 0.0,
            "大单净流入": "0", "大单净比(%)": 0.0,
            "中单净流入": "0", "中单净比(%)": 0.0,
            "小单净流入": "0", "小单净比(%)": 0.0,
        }

        output = await self._render(monkeypatch, empty)

        assert "盘中实时数据暂时不可用" in output
        assert "今日主力净流入: 0" not in output

    @pytest.mark.asyncio
    async def test_real_values_are_still_rendered(self, monkeypatch):
        """有值时行为不变。"""
        live = {
            "标的名称": "三环集团(300408)",
            "主力净流入": "1.0477亿", "主力净比(%)": 3.37,
            "超大单净流入": "-4725.87万", "超大单净比(%)": -1.0,
            "大单净流入": "2.06亿", "大单净比(%)": 4.37,
            "中单净流入": "-8426.28万", "中单净比(%)": -1.79,
            "小单净流入": "-7447.11万", "小单净比(%)": -1.58,
        }

        output = await self._render(monkeypatch, live)

        assert "标的名称: 三环集团(300408)" in output
        assert "1.0477亿" in output
        assert "3.37%" in output
