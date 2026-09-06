"""兜底 K 线的取回来之后要做的事：列名归一、派生列、成交量单位、补当日 bar。

从 ``cn_stock_source`` 里搬出来，好让 provider 能直接 import——不然新写一个源
（同花顺、雪球）要么去 import 那个几千行的模块（循环依赖），要么自己抄一份归一
逻辑（各抄各的，很快就不一致了）。这里不 import 任何本包的模块，谁都能用。

搬家没有改动任何逻辑，只是换了个位置。
"""

import logging

logger = logging.getLogger("finmcp")


# 指数的六位码：沪市个股是 60/68 开头，000 开头的六位码只可能是指数；深市指数
# 一律 399 开头，北交所是 899。用结构规则而不是 finmcp/confs/indices.json 的名单，是因为
# 那份名单只列了主要指数，而在成交量单位这件事上漏判一个就是 100 倍的量级错误。
_INDEX_CODE_PREFIXES = {"sh": ("000",), "sz": ("399",), "bj": ("899",)}


def _is_index_code(prefixed_code: str) -> bool:
    """带市场前缀的六位码是不是指数。只用于成交量单位推断。"""
    normalized = (prefixed_code or "").lower()
    market, code = normalized[:2], normalized[2:]
    return code.startswith(_INDEX_CODE_PREFIXES.get(market, ()))


def _normalize_volume_to_lots(
    frame, code: str, source: str = "腾讯", *, is_index: bool = False
):
    """Return the frame with 成交量 expressed in 手.

    The fallback providers disagree on the unit -- AkShare's Tencent endpoint
    reports 股 for some code prefixes and 手 for the rest, and Sina reports 股 --
    so neither can be assumed. Decide from the data instead: 成交额 / 收盘价 is
    the traded share count, which sits two orders of magnitude away from the lot
    count. Using 收盘价 as a stand-in for VWAP is off by a few percent at worst,
    far inside that gap. Validated against 1497 eastmoney-sourced trading days.

    指数走不了这条推断：那里的"收盘"是点位不是股价，成交额/点位 算不出任何股数。
    实测 2026-09-04 的 11 个主要指数，ratio 在 0.15 到 7.7 之间连续分布，没有一个
    靠近 1 或 0.01，却全部被判成"股"又除以 100——指数成交量因此小了两个数量级。

    前复权序列上 ratio 会被复权因子整体压低：收盘被缩放过，成交额没有。所以同一
    个标的的 qfq 与不复权两帧会给出不同的 ratio。实测 2026-09-04 两年窗口：

    | 标的 | ratio(qfq) | ratio(none) | 压低 |
    |------|-----------|------------|------|
    | SH512480 半导体ETF | 0.5007 | 0.9999 | 2.00x |
    | SZ002594 比亚迪 | 0.9900 | 0.9991 | 1.01x |
    | SZ000651 格力电器 | 0.0092 | 0.0100 | 1.09x |

    压低倍数取的是窗口内复权因子的中位数，所以除权发生在窗口靠后时才显著——
    512480 的 2.00 倍来自它那次 1:2 拆分，比亚迪最老一根的因子是 0.3263，但因为
    拆分靠窗口起点，中位数仍接近 1。两簇本身相距 71 倍（0.01 vs 0.705-1.001），
    压低 2 倍还剩 5 倍余量，所以没有改判。

    更重要的是**压低不会导致静默错判**：真要压低到 10 倍以上才会越过 0.1 判成
    "手"，而那时 ``ratio * 100`` 会大于 2.0，正好落在下面的告警区间之外，一定会
    打出 WARNING。所以这里不改判定逻辑（它承载全部兜底成交量），靠告警兜住。
    """
    volume = frame["成交量"]
    usable = (volume > 0) & (frame["成交额"] > 0) & (frame["收盘"] > 0)
    if not usable.any():
        # Only rows with no traded volume, where the unit cannot matter.
        return frame

    if is_index:
        # 腾讯的指数成交量实测是手，与东财主源一致，所以原样返回。还能做的唯一
        # 校验是把它按手换算回股：成交额/股数应当是一个说得通的成分股均价，实测
        # 11 个指数落在 16-94 元，按股算则是 1600-9400 元，A 股没有这样的均价。
        # 腾讯哪天改了单位，这条会叫出来。
        avg_price = float(
            (frame.loc[usable, "成交额"] / (volume[usable] * 100)).median()
        )
        log = logger.debug if 1.0 <= avg_price <= 1000.0 else logger.warning
        log(
            "%s历史行情指数成交量按手处理 %s: 成分股均价=%.4g 元",
            source,
            code,
            avg_price,
        )
        return frame

    implied_shares = frame.loc[usable, "成交额"] / frame.loc[usable, "收盘"]
    ratio = float((volume[usable] / implied_shares).median())
    # ~1 means the column counts 股, ~0.01 means 手; 0.1 is the log midpoint.
    in_shares = ratio > 0.1
    if not 0.5 <= (ratio if in_shares else ratio * 100) <= 2.0:
        logger.warning(
            "%s历史行情成交量量级异常 %s: ratio=%.4g，按%s处理",
            source,
            code,
            ratio,
            "股" if in_shares else "手",
        )
    else:
        logger.debug(
            "%s历史行情成交量单位 %s: ratio=%.4g unit=%s",
            source,
            code,
            ratio,
            "股" if in_shares else "手",
        )
    if in_shares:
        frame = frame.copy()
        frame["成交量"] = volume / 100
    return frame


FALLBACK_FRAME_COLUMNS = [
    "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额",
    "振幅", "涨跌幅", "涨跌额", "换手率",
]
_FALLBACK_REQUIRED = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
# A provider that raises one of these has no series for the symbol at all, as
# opposed to having none inside the requested window. Tencent raises KeyError
# for most Beijing-exchange codes and IndexError for convertible bonds; Sina
# raises KeyError or a JSON decode error. Telling the two apart is what lets the
# tools say "数据源不支持" instead of the misleading "未找到...数据".
_UNSUPPORTED_ERRORS = (KeyError, IndexError, ValueError)


def _finalize_fallback_frame(
    frame, code: str, requested_start, source: str, *, is_index: bool = False
):
    """Bring a fallback provider's frame to the shape the primary path produces."""
    import pandas as pd

    if any(column not in frame.columns for column in _FALLBACK_REQUIRED):
        logger.warning("%s历史行情字段不完整 %s: %s", source, code, list(frame.columns))
        return None
    frame = frame.copy()
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce").dt.date
    for column in _FALLBACK_REQUIRED[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    previous_close = frame["收盘"].shift(1)
    frame["涨跌额"] = (frame["收盘"] - previous_close).fillna(0.0)
    frame["涨跌幅"] = ((frame["收盘"] / previous_close - 1) * 100).fillna(0.0)
    frame["振幅"] = (((frame["最高"] - frame["最低"]) / previous_close) * 100).fillna(0.0)
    if "换手率" in frame.columns:
        # Both fallbacks report turnover as a fraction; the reports want percent.
        frame["换手率"] = pd.to_numeric(frame["换手率"], errors="coerce").fillna(0.0) * 100
    else:
        frame["换手率"] = 0.0
    frame = frame.dropna(subset=_FALLBACK_REQUIRED)
    frame = _normalize_volume_to_lots(frame, code, source, is_index=is_index)
    # 派生列算完再裁回请求区间，前置行只用于提供首行的前收盘价。
    frame = frame[frame["日期"] >= requested_start]
    if frame.empty:
        return None
    return frame[FALLBACK_FRAME_COLUMNS]


def _as_date(value):
    """把 ``YYYY-MM-DD`` 字符串、datetime 或 date 统一成 date；解析不了返回 None。"""
    import datetime as _dt

    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    try:
        return _dt.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def append_intraday_bar(frame, quote, *, adjust: str = "qfq", not_after=None):
    """把当天这根未完成的 bar 追加到兜底源的日 K 上。

    东财的 K 线接口盘中带当天，腾讯和新浪的日 K 不带（实测 2026-09-04 盘中最后
    一行仍是 09-03）。于是东财一失败，报告的"当日"就退回昨天，而同一份报告里的
    市值又是今天的——两块数据来自不同日期，报告本身却没有任何提示。

    只在行情自报的日期确实晚于表里最后一行时才追加：不看本地时钟，也就不需要
    交易日历。休市时行情的日期就是上一个交易日，与最后一行相同，自然不追加。

    ``not_after`` 是调用方请求的截止日（``research.load_raw_data`` 的 end_date）。
    没有它的话，一次 ``date=2026-08-27`` 的查询会拿到截到 08-27 的序列，再被今天
    的实时行情续上一根，于是"数据日期"变成今天、5/20/60 日窗口也跟着漂——历史
    查询被实时数据污染。不指定日期时 end_date 是"明天"，当天这根自然通得过。

    后复权序列不能这么补：那种序列的最新价是被缩放过的，而行情是原始价。前复权
    的最近若干根本来就等于原始价，所以可以直接接上。
    """
    import datetime as _dt

    import pandas as pd

    if frame is None or frame.empty or quote is None:
        return frame
    if adjust == "hfq" or not quote.has_ohlc or not quote.as_of:
        return frame
    try:
        quote_date = _dt.datetime.strptime(quote.as_of[:8], "%Y%m%d").date()
    except ValueError:
        return frame

    limit = _as_date(not_after)
    if limit is not None and quote_date > limit:
        return frame

    last_date = frame["日期"].iloc[-1]
    if quote_date <= last_date:
        return frame

    previous_close = float(frame["收盘"].iloc[-1])
    if previous_close <= 0:
        return frame

    row = {
        "日期": quote_date,
        "开盘": quote.open,
        "收盘": quote.last,
        "最高": quote.high,
        "最低": quote.low,
        "成交量": quote.volume_lots or 0.0,
        "成交额": quote.amount_yuan or 0.0,
        "振幅": (quote.high - quote.low) / previous_close * 100,
        "涨跌幅": (quote.last / previous_close - 1) * 100,
        "涨跌额": quote.last - previous_close,
        "换手率": quote.turnover_pct or 0.0,
    }
    logger.debug(
        "补当日盘中 bar 来源=%s 日期=%s 收盘=%s 前收=%s",
        quote.source,
        quote_date,
        quote.last,
        previous_close,
    )
    return pd.concat(
        [frame, pd.DataFrame([row], columns=FALLBACK_FRAME_COLUMNS)],
        ignore_index=True,
    )


def _market_prefixed_symbol(code: str, symbol: str = None) -> str:
    """Return the lower-case market-prefixed code the fallback providers expect."""
    normalized = (symbol or "").lower()
    if normalized:
        return normalized
    if code.startswith(("4", "8", "92")):
        return f"bj{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"

