"""
研究分析模块单元测试
"""

import datetime

import numpy as np
import pytest

from qtf_mcp.research import (
    compute_kdj,
    compute_macd,
    est_fin_ratio,
    filter_sector,
    is_stock,
    yearly_fin_index,
)


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

