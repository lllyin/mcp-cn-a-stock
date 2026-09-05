"""腾讯财经。

代码写法：小写带市场前缀，``sh600519`` / ``sz399006`` / ``bj920021``。
成交量单位：**股**，个股、ETF、指数一律如此（归一时由 ``_normalize_volume_to_lots``
按数据自己推断，不能硬写，因为不同接口不一致）。

已知的坑：
- 北交所代码大半抛 KeyError，但不是全部。所以这里**没有**用 supports() 排除 bj——
  排掉会把腾讯本来能给的那部分改判给别人，那是换数据源不是等价重构。要开这个优化
  得先逐个 bj 代码测一遍腾讯到底认哪些。
- 创业板指（399006）的成交量比东财/同花顺低约 3.5%、成交额低约 0.76%，整条序列都
  偏。上证/深证/科创50 逐位一致，只有它有分歧。已记进 KNOWN_DIFFERENCES。
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import platform as pf
from ..kline_frame import _finalize_fallback_frame, _is_index_code

logger = logging.getLogger("finmcp")

_COLUMN_MAP = {
    "date": "日期", "open": "开盘", "close": "收盘", "high": "最高",
    "low": "最低", "volume": "成交量", "amount": "成交额", "turnover": "换手率",
}


class TencentPlatform(pf.Platform):
    name, label = "tencent", "腾讯"
    capabilities = frozenset({"kline"})

    def fetch_kline(self, request):
        import akshare as ak

        frame = ak.stock_zh_a_hist_tx(
            symbol=request.prefixed,
            start_date=request.fetch_start,
            end_date=request.end_date.replace("-", ""),
            adjust="" if request.adjust == "none" else request.adjust,
        )
        if frame is None or frame.empty:
            return None
        frame = frame.rename(columns=_COLUMN_MAP).copy()
        required = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
        if any(column not in frame.columns for column in required):
            logger.warning("腾讯历史行情字段不完整 %s: %s", request.code, list(frame.columns))
            return None
        return _finalize_fallback_frame(
            frame, request.code, request.requested_start, self.label,
            is_index=_is_index_code(request.prefixed),
        )


pf.register(TencentPlatform())
