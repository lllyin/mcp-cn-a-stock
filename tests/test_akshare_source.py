"""
AkShare 数据源单元测试
"""

import datetime

import numpy as np
import pandas as pd
import pytest

from qtf_mcp.datasource.akshare_source import AkShareDataSource


class TestSymbolConversion:
    """测试股票代码转换"""

    @pytest.fixture
    def datasource(self):
        return AkShareDataSource()

    def test_sh_to_akshare(self, datasource):
        """上海股票代码转换"""
        code, market = datasource._symbol_to_akshare("SH600000")
        assert code == "600000"
        assert market == "sh"

    def test_sz_to_akshare(self, datasource):
        """深圳股票代码转换"""
        code, market = datasource._symbol_to_akshare("SZ000001")
        assert code == "000001"
        assert market == "sz"

    def test_gem_to_akshare(self, datasource):
        """创业板代码转换"""
        code, market = datasource._symbol_to_akshare("SZ300750")
        assert code == "300750"
        assert market == "sz"

    def test_star_market_to_akshare(self, datasource):
        """科创板代码转换"""
        code, market = datasource._symbol_to_akshare("SH688001")
        assert code == "688001"
        assert market == "sh"

    def test_infer_sh(self, datasource):
        """推断上海股票"""
        code, market = datasource._symbol_to_akshare("600000")
        assert code == "600000"
        assert market == "sh"

    def test_infer_sz(self, datasource):
        """推断深圳股票"""
        code, market = datasource._symbol_to_akshare("000001")
        assert code == "000001"
        assert market == "sz"

    def test_akshare_to_symbol_sh(self, datasource):
        """akshare 格式转内部格式 - 上海"""
        symbol = datasource._akshare_to_symbol("600000", "sh")
        assert symbol == "SH600000"

    def test_akshare_to_symbol_sz(self, datasource):
        """akshare 格式转内部格式 - 深圳"""
        symbol = datasource._akshare_to_symbol("000001", "sz")
        assert symbol == "SZ000001"

    def test_akshare_to_symbol_market_1(self, datasource):
        """akshare 格式转内部格式 - 市场代码 1"""
        symbol = datasource._akshare_to_symbol("600000", "1")
        assert symbol == "SH600000"

    def test_akshare_to_symbol_infer(self, datasource):
        """推断交易所"""
        assert datasource._akshare_to_symbol("600000") == "SH600000"
        assert datasource._akshare_to_symbol("000001") == "SZ000001"
        assert datasource._akshare_to_symbol("300750") == "SZ300750"
        assert datasource._akshare_to_symbol("688001") == "SH688001"


class TestDateToNs:
    """测试日期转纳秒时间戳"""

    @pytest.fixture
    def datasource(self):
        return AkShareDataSource()

    def test_string_date(self, datasource):
        """字符串日期"""
        ns = datasource._date_to_ns("2024-01-15")
        dt = datetime.datetime.fromtimestamp(ns / 1e9)
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15

    def test_string_date_with_time(self, datasource):
        """带时间的字符串日期"""
        ns = datasource._date_to_ns("2024-01-15 10:30:00")
        dt = datetime.datetime.fromtimestamp(ns / 1e9)
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15

    def test_datetime_object(self, datasource):
        """datetime 对象"""
        input_dt = datetime.datetime(2024, 6, 30, 12, 0, 0)
        ns = datasource._date_to_ns(input_dt)
        result_dt = datetime.datetime.fromtimestamp(ns / 1e9)
        assert result_dt.year == 2024
        assert result_dt.month == 6
        assert result_dt.day == 30

    def test_pandas_timestamp(self, datasource):
        """pandas Timestamp"""
        ts = pd.Timestamp("2024-03-15")
        ns = datasource._date_to_ns(ts)
        dt = datetime.datetime.fromtimestamp(ns / 1e9)
        assert dt.year == 2024
        assert dt.month == 3
        assert dt.day == 15


class TestParseNumericColumn:
    """测试解析数值列"""

    @pytest.fixture
    def datasource(self):
        return AkShareDataSource()

    def test_plain_numbers(self, datasource):
        """普通数字"""
        series = pd.Series([1.5, 2.5, 3.5])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [1.5, 2.5, 3.5])

    def test_integer_numbers(self, datasource):
        """整数"""
        series = pd.Series([1, 2, 3])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [1.0, 2.0, 3.0])

    def test_string_numbers(self, datasource):
        """字符串数字"""
        series = pd.Series(["1.5", "2.5", "3.5"])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [1.5, 2.5, 3.5])

    def test_percentage(self, datasource):
        """百分比格式"""
        series = pd.Series(["24.00%", "15.50%", "-5.25%"])
        result = datasource._parse_numeric_column(series, is_percent=True)
        np.testing.assert_array_almost_equal(result, [24.0, 15.5, -5.25])

    def test_wan_unit(self, datasource):
        """万单位"""
        series = pd.Series(["100万", "200.5万", "50万"])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [1e6, 2.005e6, 5e5])

    def test_yi_unit(self, datasource):
        """亿单位"""
        series = pd.Series(["1.5亿", "2亿", "0.5亿"])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [1.5e8, 2e8, 0.5e8])

    def test_empty_string(self, datasource):
        """空字符串"""
        series = pd.Series(["", "1.0", ""])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [0.0, 1.0, 0.0])

    def test_dash_placeholder(self, datasource):
        """破折号占位符"""
        series = pd.Series(["-", "1.0", "--"])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [0.0, 1.0, 0.0])

    def test_none_values(self, datasource):
        """None 值"""
        series = pd.Series([None, 1.0, None])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [0.0, 1.0, 0.0])

    def test_nan_values(self, datasource):
        """NaN 值"""
        series = pd.Series([np.nan, 1.0, np.nan])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [0.0, 1.0, 0.0])

    def test_mixed_types(self, datasource):
        """混合类型"""
        series = pd.Series([100, "200", "300万", "-", 500.5])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [100, 200, 3e6, 0, 500.5])

    def test_negative_with_unit(self, datasource):
        """负数带单位"""
        series = pd.Series(["-1.5亿", "-100万"])
        result = datasource._parse_numeric_column(series)
        np.testing.assert_array_almost_equal(result, [-1.5e8, -1e6])


class TestDataSourceName:
    """测试数据源名称"""

    def test_name(self):
        ds = AkShareDataSource()
        assert ds.name == "AkShare"

