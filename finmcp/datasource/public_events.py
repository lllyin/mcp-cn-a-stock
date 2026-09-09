"""Leakage-safe public A-share event pools backed by AkShare."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from datetime import date as date_type
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from pydantic import BaseModel, Field

from .. import cache
from ..config import CACHE_INTRADAY_TTL_SECONDS


PublicEventSource = Literal[
    "limit_up",
    "strong",
    "previous_limit_up",
    "broken_board",
    "lhb",
    "announcements",
    "earnings_forecast",
]
ALLOWED_PUBLIC_EVENT_SOURCES = {
    "limit_up",
    "strong",
    "previous_limit_up",
    "broken_board",
    "lhb",
    "announcements",
    "earnings_forecast",
}
_PUBLIC_EVENT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="public-events")
_PUBLIC_EVENT_CONCURRENCY_ATTR = "_cn_stock_public_event_concurrency"
_DIRECT_REQUESTS_LOCAL = threading.local()
# 全市场业绩预告一个报告期就有五千条量级，上一版 1000 的上限会让调用方直接失败
# （2026-09-03 就有一次 max_rows_per_source=4000 被拒）。仍然保留上限：单次响应
# 已经出现过 1.8 MB，无界会直接威胁 500 MiB 的峰值内存约束。
MAX_ROWS_PER_SOURCE_LIMIT = 10000


class PublicEventRecord(BaseModel):
    source: PublicEventSource
    event_date: str = Field(..., description="Event/as-of date in YYYY-MM-DD format")
    symbol: str = Field(..., description="Normalized SH/SZ/BJ A-share symbol")
    name: str = ""
    industry: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    interpretation: Optional[str] = None
    day_return_pct: Optional[float] = None
    turnover_pct: Optional[float] = None
    float_market_cap: Optional[float] = None
    total_market_cap: Optional[float] = None
    seal_amount: Optional[float] = None
    first_event_time: Optional[str] = None
    last_event_time: Optional[str] = None
    break_count: Optional[int] = None
    consecutive_limit_count: Optional[int] = None
    lhb_net_buy_amount: Optional[float] = None
    lhb_buy_amount: Optional[float] = None
    lhb_sell_amount: Optional[float] = None
    lhb_turnover_amount: Optional[float] = None
    market_total_amount: Optional[float] = None
    net_buy_to_market_pct: Optional[float] = None
    url: Optional[str] = None
    report_period: Optional[str] = None
    forecast_metric: Optional[str] = None
    forecast_type: Optional[str] = None
    forecast_amount: Optional[float] = None
    forecast_change_pct: Optional[float] = None
    previous_period_amount: Optional[float] = None
    forecast_content: Optional[str] = None
    change_reason: Optional[str] = None


class PublicEventSourceStatus(BaseModel):
    source: PublicEventSource
    status: Literal["SUCCESS", "FAILED"]
    raw_row_count: int = 0
    matched_row_count: int = 0
    returned_row_count: int = 0
    error: Optional[str] = None


class PublicEventPoolResponse(BaseModel):
    query_date: str
    fetched_at: str
    as_of_safe: bool = Field(
        True,
        description="True because supplier post-event return fields are never returned",
    )
    revision_safe: bool = Field(
        True,
        description="False when a provider snapshot may overwrite historical revisions",
    )
    sources_requested: list[PublicEventSource]
    source_statuses: list[PublicEventSourceStatus]
    events: list[PublicEventRecord]
    warnings: list[str] = Field(default_factory=list)


def parse_public_event_sources(value: str) -> list[PublicEventSource]:
    sources = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not sources:
        raise ValueError("sources 至少包含一个事件源")
    unknown = [source for source in sources if source not in ALLOWED_PUBLIC_EVENT_SOURCES]
    if unknown:
        raise ValueError(f"不支持的事件源: {','.join(unknown)}")
    return list(dict.fromkeys(sources))  # type: ignore[return-value]


def normalize_query_date(value: str) -> tuple[str, str]:
    raw = value.strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            parsed = datetime.strptime(raw, pattern)
            return parsed.strftime("%Y-%m-%d"), parsed.strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError("date 必须是 YYYY-MM-DD 或 YYYYMMDD")


async def get_public_market_events(
    date: str,
    sources: str = "lhb,limit_up,announcements",
    announcement_lookback_days: int = 1,
    keywords: str = "",
    max_rows_per_source: int = 1000,
    symbols: str = "",
) -> PublicEventPoolResponse:
    parsed_sources = parse_public_event_sources(sources)
    if not 1 <= announcement_lookback_days <= 5:
        raise ValueError("announcement_lookback_days 必须在 1-5 之间")
    if not 1 <= max_rows_per_source <= MAX_ROWS_PER_SOURCE_LIMIT:
        raise ValueError(
            f"max_rows_per_source 必须在 1-{MAX_ROWS_PER_SOURCE_LIMIT} 之间"
        )
    iso_date, compact_date = normalize_query_date(date)
    keyword_list = [item.strip().lower() for item in keywords.split(",") if item.strip()]
    symbol_filter = {
        item.strip().upper()
        for item in symbols.split(",")
        if item.strip()
    }
    invalid_symbols = [
        symbol for symbol in symbol_filter
        if len(symbol) != 8 or symbol[:2] not in {"SH", "SZ", "BJ"} or not symbol[2:].isdigit()
    ]
    if invalid_symbols:
        raise ValueError(f"symbols 包含非法证券代码: {','.join(sorted(invalid_symbols))}")
    loop = asyncio.get_running_loop()
    concurrency = _get_public_event_concurrency(loop)
    async with concurrency:
        return await loop.run_in_executor(
            _PUBLIC_EVENT_EXECUTOR,
            fetch_public_market_events_sync,
            iso_date,
            compact_date,
            parsed_sources,
            announcement_lookback_days,
            keyword_list,
            max_rows_per_source,
            None,
            symbol_filter,
        )


def _get_public_event_concurrency(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    semaphore = getattr(loop, _PUBLIC_EVENT_CONCURRENCY_ATTR, None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(3)
        setattr(loop, _PUBLIC_EVENT_CONCURRENCY_ATTR, semaphore)
    return semaphore


def fetch_public_market_events_sync(
    iso_date: str,
    compact_date: str,
    sources: list[PublicEventSource],
    announcement_lookback_days: int,
    keywords: list[str],
    max_rows_per_source: int,
    ak_module: Any = None,
    symbols: Optional[set[str]] = None,
) -> PublicEventPoolResponse:
    use_direct_requests = ak_module is None
    if ak_module is None:
        import akshare as ak_module

    statuses: list[PublicEventSourceStatus] = []
    warnings: list[str] = []
    output: list[PublicEventRecord] = []
    for source in sources:
        stale_days: list = []
        missing_days: list = []
        try:
            frame = _fetch_source(
                ak_module,
                source,
                compact_date,
                announcement_lookback_days,
                use_direct_requests,
                # 注入了 ak_module 的是测试路径，别把桩数据腌进进程级缓存。
                use_cache=use_direct_requests,
                stale_days=stale_days,
                missing_days=missing_days,
            )
            records = _normalize_source(source, frame, iso_date)
            raw_count = len(records)
            if keywords:
                records = [record for record in records if _matches_keywords(record, keywords)]
            if symbols:
                records = [record for record in records if record.symbol in symbols]
            matched_count = len(records)
            if matched_count > max_rows_per_source:
                warnings.append(
                    f"{source} 匹配 {matched_count} 条，按稳定顺序仅返回前 {max_rows_per_source} 条"
                )
                records = records[:max_rows_per_source]
            output.extend(records)
            statuses.append(PublicEventSourceStatus(
                source=source,
                status="SUCCESS",
                raw_row_count=raw_count,
                matched_row_count=matched_count,
                returned_row_count=len(records),
            ))
            if missing_days:
                days = "、".join(sorted(d for d, _ in missing_days))
                warnings.append(
                    f"{source} 有 {len(missing_days)} 天没取到（{days}）；"
                    f"下面是其余几天的结果，池子不完整"
                )
            if stale_days:
                oldest = max(age for _, age in stale_days) / 60.0
                warnings.append(
                    f"{source} 有 {len(stale_days)} 天用的是最旧 {oldest:.0f} 分钟前的缓存值；"
                    "上游此刻取不到，数据可能不含之后新增的事件"
                )
            if raw_count == 0:
                warnings.append(
                    f"{source} 在 {iso_date} 返回空结果；公开池可能存在历史保留窗口，空结果不代表当日无事件"
                )
            elif matched_count == 0 and keywords:
                warnings.append(f"{source} 在 {iso_date} 有 {raw_count} 条原始事件，但关键词未匹配")
        except Exception as error:  # noqa: BLE001 - preserve per-source failure
            statuses.append(PublicEventSourceStatus(
                source=source,
                status="FAILED",
                error=str(error),
            ))
    output.sort(key=lambda event: (event.source, event.event_date, event.symbol, event.title or ""))
    revision_safe = "earnings_forecast" not in sources
    if not revision_safe:
        warnings.append(
            "earnings_forecast 为报告期当前快照，可能覆盖历史修订；严格回放必须读取当日自有归档"
        )
    return PublicEventPoolResponse(
        query_date=iso_date,
        fetched_at=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        revision_safe=revision_safe,
        sources_requested=sources,
        source_statuses=statuses,
        events=output,
        warnings=warnings,
    )


# ── 缓存：按 (源, 日期) ────────────────────────────────────────────
#
# 不按整次请求缓：keywords / symbols / max_rows_per_source 都是**本地过滤参数**，
# 进 key 就是无界 key 空间（自由文本）。按 (源, 日期) 缓，key 空间有界，而且公告
# 按天缓之后 lookback=3 和 lookback=5 两次查询能共用条目，第二次只补 2 天。
#
# 缓的是**上游那张原始表**，不是归一后的记录：归一要用查询日期做兜底日期，缓在
# 归一之后的话，同一天的条目被另一个查询日期命中就会带着错的兜底值。原始表
# 经 JSON 往返不影响归一结果（实测 lhb 68 条逐字段相同）。
CACHE_NAMESPACE = "market_events"

cache.register_namespace(cache.Namespace(
    name=CACHE_NAMESPACE,
    max_entries=64,
    epoch_bound=True,
    ttl_seconds=CACHE_INTRADAY_TTL_SECONDS,
    disk=True,
    encode=lambda frame: json.loads(
        json.dumps(frame.to_dict(orient="records"), default=str)),
    decode=lambda payload: pd.DataFrame(payload),
    cacheable=lambda frame, key: frame is not None and not frame.empty,
))


def _epoch_for(source: PublicEventSource, day: str) -> Optional[str]:
    """这一天的这个源该挂在哪个纪元下。

    过去日期的池子**永远不会再变**，所以给一个恒定的纪元 token，永不失效。
    当天的还在盘后陆续发布，跟当前市场纪元走。

    ``earnings_forecast`` 例外：它是报告期的**当前快照**，会被后续修订覆盖
    （这也是 ``revision_safe=False`` 的由来），所以不给它恒定纪元。
    """
    if source == "earnings_forecast":
        return None
    try:
        queried = datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return None
    return f"date-{queried.isoformat()}" if queried < date.today() else None


def _fetch_source_day(
    ak_module: Any,
    source: PublicEventSource,
    compact_date: str,
    use_direct_requests: bool,
) -> pd.DataFrame:
    """一个源、一天、一次上游调用。缓存的最小单位就是它。"""
    def call(name: str, **kwargs) -> pd.DataFrame:
        function = getattr(ak_module, name)
        return _call_with_direct_requests(function, **kwargs) if use_direct_requests else function(**kwargs)

    if source == "limit_up":
        return call("stock_zt_pool_em", date=compact_date)
    if source == "strong":
        return call("stock_zt_pool_strong_em", date=compact_date)
    if source == "previous_limit_up":
        return call("stock_zt_pool_previous_em", date=compact_date)
    if source == "broken_board":
        return call("stock_zt_pool_zbgc_em", date=compact_date)
    if source == "lhb":
        return call("stock_lhb_detail_em", start_date=compact_date, end_date=compact_date)
    if source == "earnings_forecast":
        frame = call("stock_yjyg_em", date=compact_date)
        if frame is None or frame.empty:
            return pd.DataFrame()
        frame = frame.copy()
        frame["报告期"] = compact_date
        return frame
    return call("stock_notice_report", symbol="全部", date=compact_date)


def _cached_day(
    ak_module: Any,
    source: PublicEventSource,
    compact_date: str,
    use_direct_requests: bool,
    *,
    use_cache: bool,
    stale_days: Optional[list] = None,
) -> pd.DataFrame:
    """一个源、一天，带缓存。**取不到**和**当天真没有**必须分开往上报。

    ``get_or_load`` 把 loader 的异常咽掉只记日志、返回 None（那是它对别的调用方的
    正当契约：回退是常态）。这里要是把 None 当成空表返回，调用方看到的就是
    ``status=SUCCESS, raw_row_count=0`` 外加一句"公开池可能有历史保留窗口"——
    源崩了却被说成当天无事件，比没有这个字段更糟。

    能这么分是因为本命名空间 ``cacheable`` 拒收空表：源当天真的没有事件时
    ``get_or_load`` 返回的是**包着空表的 Entry**，只有取数失败才给 None。

    ``entry.fresh=False`` 是软过期后刷新失败、继续用旧值——数据是好的，但源此刻
    同样是崩的，按 ``get_or_load`` 的约定要标进输出，记进 ``stale_days``。
    """
    if not use_cache:
        return _fetch_source_day(ak_module, source, compact_date, use_direct_requests)

    failure: list[Exception] = []

    def load() -> pd.DataFrame:
        try:
            return _fetch_source_day(ak_module, source, compact_date, use_direct_requests)
        except Exception as error:
            failure.append(error)          # get_or_load 只会记日志，原始异常在这里留一份
            raise

    entry = cache.get_or_load(
        CACHE_NAMESPACE,
        f"{source}:{compact_date}",
        load,
        epoch=_epoch_for(source, compact_date),
    )
    if entry is not None:
        if not entry.fresh and stale_days is not None:
            stale_days.append((compact_date, entry.age_seconds))
        return entry.value
    if failure:
        raise failure[0]
    # 没抛异常又没结果：上游给了 None。同样是没取到，不是当天没有。
    raise RuntimeError(f"{source} {compact_date} 上游返回空值")


def _fetch_source(
    ak_module: Any,
    source: PublicEventSource,
    compact_date: str,
    announcement_lookback_days: int,
    use_direct_requests: bool,
    *,
    use_cache: bool = False,
    stale_days: Optional[list] = None,
    missing_days: Optional[list] = None,
) -> pd.DataFrame:
    """一个源在这次查询里要的全部原始数据，按天取、按天缓，最后拼起来。

    公告按 lookback 天逐天取。**某一天取不到时照常返回其余几天**，同时把缺了哪天
    记进 ``missing_days``，由调用方讲出来。

    两种极端都不对：静默丢掉那一天，就是悄悄给了个更小的池子；为一天失败把整个源
    判失败、一条不返回，是拿到手的数据又扔掉——同样让数据变少（AGENTS §一）。
    全部天都取不到才是整源失败。
    """
    def day(value: str) -> pd.DataFrame:
        return _cached_day(ak_module, source, value, use_direct_requests,
                           use_cache=use_cache, stale_days=stale_days)

    if source == "earnings_forecast":
        report_period = _latest_completed_report_period(compact_date)
        frame = day(report_period)
        if frame is None or frame.empty:
            return pd.DataFrame()
        notice_date = pd.to_datetime(frame.get("公告日期"), errors="coerce")
        end = datetime.strptime(compact_date, "%Y%m%d")
        start = end - timedelta(days=announcement_lookback_days - 1)
        return frame[(notice_date >= start) & (notice_date <= end)]

    if source != "announcements":
        return day(compact_date)

    # 公告本来就是按天取的，逐天缓存于是天然让不同 lookback 的查询共用条目。
    anchor = datetime.strptime(compact_date, "%Y%m%d")
    frames, failures = [], []
    for offset in range(announcement_lookback_days):
        value = (anchor - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            frames.append(day(value))
        except Exception as error:
            if missing_days is None:      # 调用方没准备接缺口，那就别把它咽了
                raise
            failures.append(value)
            missing_days.append((value, str(error)))
    if failures and len(failures) == announcement_lookback_days:
        raise RuntimeError(f"公告 {announcement_lookback_days} 天全部取不到")
    frames = [f for f in frames if f is not None and not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _call_with_direct_requests(function: Any, **kwargs) -> pd.DataFrame:
    """Call one AkShare function without mutating its globally patched requests.get."""
    function_object = getattr(function, "__func__", function)
    globals_copy = dict(function_object.__globals__)
    globals_copy["requests"] = types.SimpleNamespace(get=_direct_session().get)
    cloned = types.FunctionType(
        function_object.__code__,
        globals_copy,
        name=function_object.__name__,
        argdefs=function_object.__defaults__,
        closure=function_object.__closure__,
    )
    return cloned(**kwargs)


def _direct_session() -> requests.Session:
    session = getattr(_DIRECT_REQUESTS_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _DIRECT_REQUESTS_LOCAL.session = session
    return session


def _normalize_source(
    source: PublicEventSource,
    frame: pd.DataFrame,
    fallback_date: str,
) -> list[PublicEventRecord]:
    if frame is None or frame.empty:
        return []
    rows = frame.to_dict("records")
    if source == "lhb":
        rows = _deduplicate_lhb(rows)
    events = []
    for row in rows:
        code = str(row.get("代码") or row.get("股票代码") or "").strip().zfill(6)
        symbol = _normalize_stock_symbol(code)
        if symbol is None:
            continue
        market_total = _number(row.get("市场总成交额"))
        net_buy = _number(row.get("龙虎榜净买额"))
        event = PublicEventRecord(
            source=source,
            event_date=_event_date(row, fallback_date),
            symbol=symbol,
            name=str(row.get("名称") or row.get("股票简称") or "").strip(),
            industry=_text(row.get("所属行业")),
            title=_event_title(source, row),
            category=_event_category(source, row),
            interpretation=_text(row.get("解读")),
            day_return_pct=_number(row.get("涨跌幅")),
            turnover_pct=_number(row.get("换手率")),
            float_market_cap=_number(row.get("流通市值")),
            total_market_cap=_number(row.get("总市值")),
            seal_amount=_number(row.get("封板资金")),
            first_event_time=_text(row.get("首次封板时间")),
            last_event_time=_text(row.get("最后封板时间")),
            break_count=_integer(row.get("炸板次数")),
            consecutive_limit_count=_integer(row.get("连板数")),
            lhb_net_buy_amount=net_buy,
            lhb_buy_amount=_number(row.get("龙虎榜买入额")),
            lhb_sell_amount=_number(row.get("龙虎榜卖出额")),
            lhb_turnover_amount=_number(row.get("龙虎榜成交额")),
            market_total_amount=market_total,
            net_buy_to_market_pct=round(net_buy / market_total * 100, 4)
            if net_buy is not None and market_total not in (None, 0) else None,
            url=_text(row.get("网址")),
            report_period=_report_period(row),
            forecast_metric=_text(row.get("预测指标")),
            forecast_type=_text(row.get("预告类型")),
            forecast_amount=_number(row.get("预测数值")),
            forecast_change_pct=_number(row.get("业绩变动幅度")),
            previous_period_amount=_number(row.get("上年同期值")),
            forecast_content=_text(row.get("业绩变动")),
            change_reason=_text(row.get("业绩变动原因")),
        )
        events.append(event)
    return events


def _deduplicate_lhb(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("代码", "")).strip().zfill(6)
        current = by_code.get(code)
        if current is None or (_number(row.get("龙虎榜成交额")) or 0) > (
            _number(current.get("龙虎榜成交额")) or 0
        ):
            by_code[code] = row
    return list(by_code.values())


def _normalize_stock_symbol(code: str) -> Optional[str]:
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"SH{code}"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"SZ{code}"
    if code.startswith(("4", "8", "92")):
        return f"BJ{code}"
    return None


def _event_date(row: dict[str, Any], fallback: str) -> str:
    for key in ("公告日期", "上榜日"):
        value = row.get(key)
        if isinstance(value, (datetime, date_type, pd.Timestamp)):
            return value.strftime("%Y-%m-%d")
        text = str(value or "").strip()
        for pattern in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(text[:10], pattern).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return fallback


def _event_title(source: PublicEventSource, row: dict[str, Any]) -> Optional[str]:
    if source == "announcements":
        return _text(row.get("公告标题"))
    if source == "earnings_forecast":
        forecast_type = _text(row.get("预告类型")) or "未知类型"
        metric = _text(row.get("预测指标")) or "未知指标"
        change = _number(row.get("业绩变动幅度"))
        suffix = f"，变动幅度中值{change:.2f}%" if change is not None else ""
        return f"业绩预告：{forecast_type}；{metric}{suffix}"
    if source == "lhb":
        return _text(row.get("上榜原因"))
    return "涨停/强势事件"


def _event_category(source: PublicEventSource, row: dict[str, Any]) -> Optional[str]:
    if source == "announcements":
        return _text(row.get("公告类型"))
    if source == "earnings_forecast":
        return "业绩预告"
    if source == "lhb":
        return "龙虎榜"
    return source


def _matches_keywords(event: PublicEventRecord, keywords: list[str]) -> bool:
    haystack = " ".join(filter(None, [
        event.name,
        event.industry,
        event.title,
        event.category,
        event.interpretation,
    ])).lower()
    return any(keyword in haystack for keyword in keywords)


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> Optional[int]:
    number = _number(value)
    return int(number) if number is not None else None


def _text(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value or "").strip()
    return text or None


def _latest_completed_report_period(compact_date: str) -> str:
    parsed = datetime.strptime(compact_date, "%Y%m%d")
    if parsed.month <= 3:
        return f"{parsed.year - 1}1231"
    if parsed.month <= 6:
        return f"{parsed.year}0331"
    if parsed.month <= 9:
        return f"{parsed.year}0630"
    return f"{parsed.year}0930"


def _report_period(row: dict[str, Any]) -> Optional[str]:
    value = str(row.get("报告期") or "").strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return None
