"""新浪财经。

代码写法：小写带市场前缀，同腾讯。

它在这条链上的价值只有一个，但不可替代：**腾讯不认的北交所代码它认**。实测
2026-09-03，腾讯对约一半的 bj 代码抛 KeyError，新浪能给且带成交额；没有这一级那些
标的会返回"未找到...数据"，而数据其实是有的。

它自己的洞：ETF（512480/159995）和创业板指走 ``stock_zh_a_daily`` 是 JSONDecodeError，
2026-09-05 复测仍然如此。也就是说那几类目前只有腾讯一条路。要补的话是换
``stock_zh_index_daily``（覆盖全但没有成交额），**单独起一个平台**，别改这个——
"没有成交额"是另一种数据形态，混在一起会让调用方分不清拿到的是哪一种。

口径上和腾讯同源：创业板指的成交量两家给出的数一字不差（193,413,042），所以它们
互相校验不了，都比东财/同花顺低 3.5%。
"""

from __future__ import annotations

import datetime
import logging

from .. import platform as pf
from ..kline_frame import _finalize_fallback_frame, _is_index_code

logger = logging.getLogger("finmcp")

_COLUMN_MAP = {
    "date": "日期", "open": "开盘", "close": "收盘", "high": "最高",
    "low": "最低", "volume": "成交量", "amount": "成交额", "turnover": "换手率",
}


class SinaPlatform(pf.Platform):
    """新浪是**一个**平台，提供两种能力。

    K 线和交易日历都归它——合成一个类不是为了少写代码，是因为"新浪"这个平台的
    事实只有一份（主机、编码、限流表现）。原先它们是两个同名类，注册时直接撞了，
    那正是这套架构该拦下的错。
    """

    name, label = "sina", "新浪"
    capabilities = frozenset({"kline", "trading_calendar"})

    def fetch_trading_calendar(self, request):
        """上交所公布的交易日名单，经 AkShare 取。

        实测 2026-09-05：8797 行、0.18 秒、约 69 KiB，覆盖 1990-12-19 至 2026-12-31。
        交易日历提前一年公布，所以缓存一天绰绰有余。
        """
        import akshare as ak

        from ..trading_calendar import Calendar

        frame = ak.tool_trade_date_hist_sina()
        if frame is None or frame.empty:
            return None
        days = frozenset(
            d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10])
            for d in frame["trade_date"]
        )
        if not days:
            return None
        return Calendar(days=days, covers_through=max(days), source=self.name)

    def fetch_kline(self, request):
        import akshare as ak

        frame = ak.stock_zh_a_daily(
            symbol=request.prefixed,
            start_date=request.fetch_start,
            end_date=request.end_date.replace("-", ""),
            adjust="" if request.adjust == "none" else request.adjust,
        )
        if frame is None or frame.empty:
            return None
        return _finalize_fallback_frame(
            frame.rename(columns=_COLUMN_MAP), request.code,
            request.requested_start, self.label,
            is_index=_is_index_code(request.prefixed),
        )


pf.register(SinaPlatform())
