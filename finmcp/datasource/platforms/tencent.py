"""腾讯财经。

代码写法：小写带市场前缀，``sh600519`` / ``sz399006`` / ``bj920021``。
成交量单位：**股**，个股、ETF、指数一律如此（归一时由 ``_normalize_volume_to_lots``
按数据自己推断，不能硬写，因为不同接口不一致）。

## 日 K 自己发请求，不经 AkShare

``ak.stock_zh_a_hist_tx`` 先打一次 ``weekTrends`` 查上市年份，再**按日历年**循环取，
每年一个请求。报告要两年历史就跨三个年份：一次取数 4 个请求，前复权加不复权 8 个。
2026-09-06 实测每个请求约 1.9s，K 线一维占了全部上游耗时的 79%
（verify 68 次、P50 8.27s；60 次/分压测里 P50 12.5s），熔断把东财跳掉之后仍然如此。

接口本身按**条数**取：一次最多 640 根、以 ``end`` 为锚往前数，``start`` 基本不起
作用（实测 count=2000 也只回 640；``2026-09-01..09-06`` 配 640 回的是 2024-01-08 起
的 640 根）。两年约 480 根，一个请求就够；超过 640 根才往前翻页，把 ``end`` 换成上
一页最早那天的前一天。

**归一逐字照抄 AkShare**（列序、``to_numeric``、成交量 ×100 的前缀规则、换手率 /100、
成交额 ×10000、去重、按日期升序、裁到区间），包括它的怪癖——``sz000`` 开头的深市
个股被它当成"已经是股"不乘 100。怪癖由下游 ``_normalize_volume_to_lots`` 按数据修正，
这里改了它反而会让两条路径产生差异。

## "腾讯认不认这个代码"也照抄 AkShare

AkShare 的 ``get_tx_start_year`` 先查 ``weekTrends``；空就再探一次 320 根并硬取
``["day"]`` 键——而个股的复权序列键是 ``qfqday``，于是 KeyError。北交所的 weekTrends
全是空的，所以"北交所代码大半抛 KeyError"其实是这一步抛的，日 K 接口本身认它们。

这里**保留同一道门**：不然北交所会从新浪改判给腾讯，而两家的口径不同（2026-09-06
实测 BJ920021：新浪成交额 586,975,792 元、腾讯 586,975,800，成交量差 1 手）。这不是
等价重构。门的结果按代码缓存在进程里（它是"腾讯有没有这只票的周线"这种静态事实），
所以稳态下每标的多付的只是第一次那一个请求：8 个请求降到 2 个。

已知的坑：
- 创业板指（399006）的成交量比东财/同花顺低约 3.5%、成交额低约 0.76%，整条序列都
  偏。上证/深证/科创50 逐位一致，只有它有分歧。已记进 KNOWN_DIFFERENCES。
- 指数没有复权序列：请求 qfq 时响应里只有 ``day``。AkShare 的取键顺序是
  ``day`` → ``hfqday`` → ``qfqday``，这里保持一致。
"""

from __future__ import annotations

import datetime
import functools
import json
import logging
from typing import Callable, Optional

from .. import platform as pf
from ..kline_frame import _finalize_fallback_frame, _is_index_code

logger = logging.getLogger("finmcp")

_COLUMN_MAP = {
    "date": "日期", "open": "开盘", "close": "收盘", "high": "最高",
    "low": "最低", "volume": "成交量", "amount": "成交额", "turnover": "换手率",
}

_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_WEEK_TRENDS_URL = "https://web.ifzq.gtimg.cn/other/klineweb/klineWeb/weekTrends"
#: 接口单次上限。给再大也只回 640 根（实测 count=2000 → 640）。
_KLINE_PAGE = 640
#: 翻页兜底：40 × 640 根约 100 年，正常请求一两页就停，这个数只防翻页停不下来。
_KLINE_MAX_PAGES = 40
#: AkShare 的规则原样保留：这些前缀的成交量它认为已经是股、不乘 100。
_VOLUME_ALREADY_SHARES = ("sh688", "sz399", "sh000", "sz000")
#: AkShare 取 8 列时跳过的原始下标 6：那一位是个占位 dict，不是数据。
_RAW_COLUMNS = [0, 1, 2, 3, 4, 5, 7, 8]
_FRAME_COLUMNS = ["date", "open", "close", "high", "low", "volume", "turnover", "amount"]


def _get_payload(url: str, params: dict) -> dict:
    """发一次请求，剥掉 JSONP 外壳。单拎出来是为了测试能不联网注入假响应。"""
    import requests

    # AkShare 不给超时，一个挂住的连接会占死线程池里的一个 worker。15s 和同花顺
    # 那个平台一致；这只改失败的形态，不改数据。
    response = requests.get(url, params=params, timeout=15)
    text = response.text
    try:
        return json.loads(text[text.find("={") + 1:])
    except json.JSONDecodeError as error:
        # JSONDecodeError 是 ValueError，平台层会把它当成"不认这个代码"；一页错误页
        # 不是那回事，换成普通异常让它只算一次失败。
        raise RuntimeError(f"腾讯响应不是 JSONP: {text[:60]!r}") from error


def _series_of(data: dict, adjust: str) -> list:
    """AkShare 的取键顺序：``day`` → ``hfqday`` → ``qfqday``。最后一个不存在就 KeyError。"""
    if "day" in data:
        return data["day"]
    if "hfqday" in data:
        return data["hfqday"]
    return data["qfqday"]


@functools.lru_cache(maxsize=4096)
def _passes_akshare_gate(symbol: str) -> bool:
    """AkShare 的 ``get_tx_start_year`` 会不会放行这个代码。

    照它的顺序：``weekTrends`` 有数据就放行；没有就探 320 根并硬取 ``["day"][0]``，
    没有这个键（个股只有 ``qfqday``）或列表为空，就是它抛 KeyError / IndexError 的
    地方——两者在平台层都算"不认这个代码"。代码不存在时 ``["data"][symbol]`` 的
    KeyError 原样冒出去。结果按代码缓存：它是静态事实，不该每次取数都付一个请求。
    """
    trends = _get_payload(_WEEK_TRENDS_URL, {
        "code": symbol, "type": "qfq", "_var": "trend_qfq", "r": "0.3506048543943414",
    })
    if trends.get("data"):
        return True
    probe = _get_payload(_KLINE_URL, {
        "_var": "kline_dayqfq", "param": f"{symbol},day,,,320,qfq", "r": "0.751892490072597",
    })
    return bool(probe["data"][symbol].get("day"))


def _fetch_kline_rows(
    symbol: str,
    start: str,
    end: str,
    adjust: str,
    *,
    get: Optional[Callable[[dict], dict]] = None,
) -> list:
    """``[start, end]``（YYYY-MM-DD，闭区间）内的原始行，按日期升序、按日期去重。

    以 ``end`` 为锚一页 640 根往前翻，直到最早那根不晚于 ``start``、或页不满、或没有
    更早的数据。腾讯不认的代码在 ``data`` 里没有那个键，这里让 KeyError 原样冒出去。
    """
    get = (lambda params: _get_payload(_KLINE_URL, params)) if get is None else get
    rows: dict = {}
    page_end = end
    for _ in range(_KLINE_MAX_PAGES):
        payload = get({
            "_var": f"kline_day{adjust}",
            "param": f"{symbol},day,{start},{page_end},{_KLINE_PAGE},{adjust}",
        })
        page = _series_of(payload["data"][symbol], adjust)
        if not page:
            break
        for row in page:
            rows.setdefault(row[0], row)
        first = page[0][0]
        if len(page) < _KLINE_PAGE or first <= start:
            break
        earlier = datetime.date.fromisoformat(first) - datetime.timedelta(days=1)
        page_end = earlier.isoformat()
    return [rows[day] for day in sorted(rows) if start <= day <= end]


def _rows_to_frame(rows: list, symbol: str, start_date: str, end_date: str):
    """原始行 → 和 ``ak.stock_zh_a_hist_tx`` 逐字相同的表。

    每一步都照它的顺序做，包括 ``pd.DataFrame(rows)`` 这一步——从字符串建表再
    ``to_numeric``，整数列才会和它一样推成 int64，而 ``kline_daily`` 打印成交量用的
    是 ``{:,}``，int 和 float 印出来不一样。``start_date`` / ``end_date`` 是 YYYYMMDD，
    和它的入参同一种写法。
    """
    import pandas as pd

    frame = pd.DataFrame(rows).iloc[:, _RAW_COLUMNS]
    frame.columns = _FRAME_COLUMNS
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in _FRAME_COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not symbol.startswith(_VOLUME_ALREADY_SHARES):
        frame["volume"] = frame["volume"] * 100
    frame["turnover"] = frame["turnover"] / 100
    frame["amount"] = frame["amount"] * 10000
    frame.drop_duplicates(inplace=True, ignore_index=True)
    frame.index = pd.to_datetime(frame["date"], errors="coerce")
    frame.sort_index(inplace=True)
    frame = frame[start_date:end_date]
    frame.reset_index(inplace=True, drop=True)
    return frame


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _history_frame(symbol: str, start_date: str, end_date: str, adjust: str):
    """``ak.stock_zh_a_hist_tx(symbol, start_date, end_date, adjust)`` 的等价替身。

    同样的入参写法（YYYYMMDD、adjust 为 ``qfq`` / ``hfq`` / 空串）、同样的返回表、
    同样的"认不认这个代码"，只是请求数不同。测试从这一层注入假数据。
    """
    if not _passes_akshare_gate(symbol):
        # 和 AkShare 抛的是同一个 KeyError：平台层据此判"不支持"，措辞才对。
        raise KeyError("day")
    rows = _fetch_kline_rows(symbol, _iso(start_date), _iso(end_date), adjust)
    if not rows:
        import pandas as pd

        return pd.DataFrame(columns=_FRAME_COLUMNS)
    return _rows_to_frame(rows, symbol, start_date, end_date)


class TencentPlatform(pf.Platform):
    name, label = "tencent", "腾讯"
    capabilities = frozenset({"kline"})

    def fetch_kline(self, request):
        frame = _history_frame(
            request.prefixed,
            request.fetch_start,
            request.end_date.replace("-", ""),
            "" if request.adjust == "none" else request.adjust,
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
