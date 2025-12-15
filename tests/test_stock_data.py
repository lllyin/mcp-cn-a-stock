"""
StockData 数据类单元测试
"""

import numpy as np
import pytest

from qtf_mcp.datasource.base import StockData


class TestStockDataInit:
    """测试 StockData 初始化"""

    def test_minimal_init(self):
        """最小初始化"""
        data = StockData(symbol="SH600000")
        assert data.symbol == "SH600000"
        assert data.name == ""
        assert len(data.date) == 0
        assert len(data.close) == 0

    def test_with_name(self):
        """带名称初始化"""
        data = StockData(symbol="SH600000", name="浦发银行")
        assert data.name == "浦发银行"

    def test_with_kline_data(self):
        """带 K 线数据初始化"""
        data = StockData(
            symbol="SH600000",
            date=np.array([1704067200000000000], dtype=np.int64),
            open=np.array([10.0], dtype=np.float64),
            high=np.array([10.5], dtype=np.float64),
            low=np.array([9.8], dtype=np.float64),
            close=np.array([10.2], dtype=np.float64),
        )
        assert len(data.date) == 1
        assert data.open[0] == 10.0
        assert data.high[0] == 10.5
        assert data.low[0] == 9.8
        assert data.close[0] == 10.2


class TestStockDataIsEmpty:
    """测试 is_empty 方法"""

    def test_empty_data(self):
        """空数据"""
        data = StockData(symbol="SH600000")
        assert data.is_empty() is True

    def test_with_data(self):
        """有数据"""
        data = StockData(
            symbol="SH600000",
            date=np.array([1704067200000000000], dtype=np.int64),
            close=np.array([10.5], dtype=np.float64),
        )
        assert data.is_empty() is False

    def test_empty_close_but_has_date(self):
        """有日期但没有收盘价"""
        data = StockData(
            symbol="SH600000",
            date=np.array([1704067200000000000], dtype=np.int64),
        )
        assert data.is_empty() is False


class TestStockDataToDict:
    """测试 to_dict 方法"""

    def test_basic_fields(self):
        """基本字段转换"""
        data = StockData(
            symbol="SH600000",
            name="浦发银行",
            date=np.array([1704067200000000000], dtype=np.int64),
            open=np.array([10.0], dtype=np.float64),
            high=np.array([10.5], dtype=np.float64),
            low=np.array([9.8], dtype=np.float64),
            close=np.array([10.2], dtype=np.float64),
            volume=np.array([1000000], dtype=np.float64),
            amount=np.array([10200000], dtype=np.float64),
        )

        d = data.to_dict()

        assert d["NAME"] == "浦发银行"
        np.testing.assert_array_equal(d["OPEN"], [10.0])
        np.testing.assert_array_equal(d["HIGH"], [10.5])
        np.testing.assert_array_equal(d["LOW"], [9.8])
        np.testing.assert_array_equal(d["CLOSE"], [10.2])
        np.testing.assert_array_equal(d["VOLUME"], [1000000])
        np.testing.assert_array_equal(d["AMOUNT"], [10200000])

    def test_close2_and_price(self):
        """CLOSE2 和 PRICE 字段"""
        data = StockData(
            symbol="SH600000",
            close=np.array([10.2], dtype=np.float64),
            close_unadj=np.array([10.0], dtype=np.float64),
        )

        d = data.to_dict()

        np.testing.assert_array_equal(d["CLOSE2"], [10.0])
        np.testing.assert_array_equal(d["PRICE"], [10.0])

    def test_sectors(self):
        """板块信息"""
        data = StockData(symbol="SH600000", sectors=["银行", "金融", "上海本地"])

        d = data.to_dict()
        assert d["SECTOR"] == ["银行", "金融", "上海本地"]

    def test_empty_sectors(self):
        """空板块信息"""
        data = StockData(symbol="SH600000")

        d = data.to_dict()
        assert d["SECTOR"] == []

    def test_finance_data(self):
        """财务数据"""
        data = StockData(
            symbol="SH600000",
            finance_date=np.array([1704067200000000000], dtype=np.int64),
            main_revenue=np.array([1e10], dtype=np.float64),
            net_profit=np.array([5e9], dtype=np.float64),
            eps=np.array([1.5], dtype=np.float64),
            nav_per_share=np.array([12.0], dtype=np.float64),
            roe=np.array([15.5], dtype=np.float64),
        )

        d = data.to_dict()

        # 检查 _DS_FINANCE 存在
        assert "_DS_FINANCE" in d
        fin, period = d["_DS_FINANCE"]
        assert period == "1q"
        np.testing.assert_array_equal(fin["EPS"], [1.5])
        np.testing.assert_array_equal(fin["ROE"], [15.5])
        np.testing.assert_array_equal(fin["NAVPS"], [12.0])
        np.testing.assert_array_equal(fin["MR"], [1e10])
        np.testing.assert_array_equal(fin["NP"], [5e9])

    def test_no_finance_data(self):
        """无财务数据时不包含 _DS_FINANCE"""
        data = StockData(symbol="SH600000")
        d = data.to_dict()
        assert "_DS_FINANCE" not in d

    def test_fund_flow_fields(self):
        """资金流向字段"""
        data = StockData(
            symbol="SH600000",
            fund_main_amount=np.array([1e8], dtype=np.float64),
            fund_main_ratio=np.array([0.15], dtype=np.float64),
            fund_xl_amount=np.array([5e7], dtype=np.float64),
            fund_xl_ratio=np.array([0.08], dtype=np.float64),
        )

        d = data.to_dict()

        np.testing.assert_array_equal(d["A_A"], [1e8])
        np.testing.assert_array_equal(d["A_R"], [0.15])
        np.testing.assert_array_equal(d["XL_A"], [5e7])
        np.testing.assert_array_equal(d["XL_R"], [0.08])

    def test_given_cash_and_share(self):
        """分红送股数据"""
        data = StockData(
            symbol="SH600000",
            given_cash=np.array([0.5], dtype=np.float64),
            given_share=np.array([0.1], dtype=np.float64),
        )

        d = data.to_dict()

        np.testing.assert_array_equal(d["GCASH"], [0.5])
        np.testing.assert_array_equal(d["GSHARE"], [0.1])

    def test_total_shares(self):
        """总股本"""
        data = StockData(
            symbol="SH600000",
            total_shares=np.array([1e10], dtype=np.float64),
        )

        d = data.to_dict()

        np.testing.assert_array_equal(d["TCAP"], [1e10])


class TestStockDataComplete:
    """完整数据测试"""

    def test_full_data_to_dict(self):
        """完整数据转换为字典"""
        n = 5
        data = StockData(
            symbol="SH600000",
            name="浦发银行",
            date=np.arange(n, dtype=np.int64) * 86400000000000 + 1704067200000000000,
            open=np.array([10.0, 10.1, 10.2, 10.3, 10.4], dtype=np.float64),
            high=np.array([10.5, 10.6, 10.7, 10.8, 10.9], dtype=np.float64),
            low=np.array([9.8, 9.9, 10.0, 10.1, 10.2], dtype=np.float64),
            close=np.array([10.2, 10.3, 10.4, 10.5, 10.6], dtype=np.float64),
            volume=np.array([1e6, 1.1e6, 1.2e6, 1.3e6, 1.4e6], dtype=np.float64),
            amount=np.array([1e7, 1.1e7, 1.2e7, 1.3e7, 1.4e7], dtype=np.float64),
            close_unadj=np.array([10.0, 10.1, 10.2, 10.3, 10.4], dtype=np.float64),
            given_cash=np.zeros(n, dtype=np.float64),
            given_share=np.zeros(n, dtype=np.float64),
            finance_date=np.array([1704067200000000000], dtype=np.int64),
            total_shares=np.array([1e10], dtype=np.float64),
            main_revenue=np.array([1e11], dtype=np.float64),
            net_profit=np.array([1e10], dtype=np.float64),
            eps=np.array([1.0], dtype=np.float64),
            nav_per_share=np.array([10.0], dtype=np.float64),
            roe=np.array([10.0], dtype=np.float64),
            sectors=["银行", "金融"],
        )

        d = data.to_dict()

        # 验证所有必要字段都存在
        required_fields = [
            "NAME",
            "DATE",
            "OPEN",
            "HIGH",
            "LOW",
            "CLOSE",
            "VOLUME",
            "AMOUNT",
            "CLOSE2",
            "PRICE",
            "SECTOR",
            "TCAP",
            "GCASH",
            "GSHARE",
            "_DS_FINANCE",
        ]

        for field in required_fields:
            assert field in d, f"Missing field: {field}"

        # 验证 K 线数据长度
        assert len(d["DATE"]) == n
        assert len(d["CLOSE"]) == n

        # 验证财务数据
        fin, period = d["_DS_FINANCE"]
        assert len(fin["DATE"]) == 1

