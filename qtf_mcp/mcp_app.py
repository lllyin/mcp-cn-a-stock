import asyncio
import datetime
import logging
import time
import uuid
from io import StringIO
from typing import Literal, Dict, List, Optional

from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import research
from .cache import build_key, get_report_cache, is_cacheable_report
from .datasource import get_datasource
from .datasource.base import FETCH_FAILURES_KEY, FetchRequirements
from .datasource.market_breadth import get_market_breadth
from .config import BATCH_QUERY_CONCURRENCY
from .observability import bind_log_context, http_trace_id_var

logger = logging.getLogger("qtf_mcp")
_active_report_requests = 0
_BATCH_QUERY_ADMISSION_ATTR = "_cn_stock_batch_query_admission"


class BatchQueryAdmission:
    """Event-loop-owned admission control shared by all report modes."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0
        self.waiting = 0
        self._semaphore = asyncio.Semaphore(limit)

    async def acquire(self) -> float:
        started_at = time.perf_counter()
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        except BaseException:
            self.waiting -= 1
            raise
        self.waiting -= 1
        self.active += 1
        return time.perf_counter() - started_at

    def release(self) -> None:
        self.active -= 1
        self._semaphore.release()


def _get_batch_query_admission() -> BatchQueryAdmission:
    loop = asyncio.get_running_loop()
    admission = getattr(loop, _BATCH_QUERY_ADMISSION_ATTR, None)
    if admission is None or admission.limit != BATCH_QUERY_CONCURRENCY:
        admission = BatchQueryAdmission(BATCH_QUERY_CONCURRENCY)
        setattr(loop, _BATCH_QUERY_ADMISSION_ATTR, admission)
    return admission


# --- Output Models for MCP Inspector Schema ---

class BatchReportResponse(BaseModel):
    """批量报表响应模型"""
    symbols_count: int = Field(..., description="成功处理并返回报表的证券标的数量")
    timestamp: str = Field(..., description="报告生成时间 (YYYY-MM-DD HH:MM:SS)")
    reports: Dict[str, str] = Field(..., description="成功生成的报表集合 (键为代码，值为 Markdown 文本)")
    errors: Dict[str, str] = Field(..., description="发生错误的标的信息 (键为代码，值为错误详情)")
    warnings: List[str] = Field(default_factory=list, description="非致命警告信息")


class KDJIndicator(BaseModel):
    """KDJ indicator values."""
    k: Optional[float] = Field(None, description="K value")
    d: Optional[float] = Field(None, description="D value")
    j: Optional[float] = Field(None, description="J value")


class MACDIndicator(BaseModel):
    """MACD indicator values."""
    dif: Optional[float] = Field(None, description="DIF value")
    dea: Optional[float] = Field(None, description="DEA value")
    histogram: Optional[float] = Field(None, description="DIF - DEA")


class RSIIndicator(BaseModel):
    """RSI indicator values."""
    rsi6: Optional[float] = Field(None, description="6-period RSI")
    rsi12: Optional[float] = Field(None, description="12-period RSI")
    rsi24: Optional[float] = Field(None, description="24-period RSI")


class BBandsIndicator(BaseModel):
    """Bollinger Bands indicator values."""
    upper: Optional[float] = Field(None, description="Upper band")
    middle: Optional[float] = Field(None, description="Middle band")
    lower: Optional[float] = Field(None, description="Lower band")


class OHLCData(BaseModel):
    """Daily OHLCV values."""
    open: Optional[float] = Field(None, description="Open price")
    close: Optional[float] = Field(None, description="Close price")
    high: Optional[float] = Field(None, description="High price")
    low: Optional[float] = Field(None, description="Low price")
    volume: Optional[float] = Field(None, description="Trading volume")


class TechnicalIndicatorItem(BaseModel):
    """Technical indicators for one trading day."""
    date: str = Field(..., description="Trading date in YYYY-MM-DD format")
    ohlc: OHLCData = Field(..., description="Daily OHLCV values")
    kdj: Optional[KDJIndicator] = Field(None, description="KDJ values")
    macd: Optional[MACDIndicator] = Field(None, description="MACD values")
    rsi: Optional[RSIIndicator] = Field(None, description="RSI values")
    bbands: Optional[BBandsIndicator] = Field(None, description="Bollinger Bands values")


class TechnicalReport(BaseModel):
    """Machine-readable technical indicator report for one symbol."""
    symbol: str = Field(..., description="Normalized stock symbol")
    name: str = Field("", description="Stock name")
    quote_date: Optional[str] = Field(None, description="Latest trading date in YYYY-MM-DD format")
    indicators: List[TechnicalIndicatorItem] = Field(default_factory=list, description="Recent technical indicators")


class BatchTechnicalResponse(BaseModel):
    """Batch technical indicator response."""
    symbols_count: int = Field(..., description="Number of requested symbols after applying batch limit")
    timestamp: str = Field(..., description="Report generation time (YYYY-MM-DD HH:MM:SS)")
    reports: Dict[str, TechnicalReport] = Field(..., description="Technical reports keyed by normalized symbol")
    errors: Dict[str, str] = Field(..., description="Errors keyed by input or normalized symbol")
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warning messages")


class MarketBreadthBucketResponse(BaseModel):
    """Number of stocks in one percentage-change range."""
    range: str = Field(..., description="Percentage-change range")
    count: int = Field(..., description="Number of stocks in this range")


class MarketBreadthResponse(BaseModel):
    """Whole-market rise/fall distribution."""
    source: str = Field(..., description="Data source that produced this response")
    fetched_at: str = Field(..., description="Fetch time in Asia/Shanghai")
    trade_date: Optional[str] = Field(None, description="Latest trading date when provided by the source")
    market_time: Optional[str] = Field(None, description="Latest intraday sample time when provided by the source")
    up_count: int = Field(..., description="Number of rising A-share stocks")
    down_count: int = Field(..., description="Number of falling A-share stocks")
    flat_count: int = Field(..., description="Number of unchanged A-share stocks")
    limit_up_count: Optional[int] = Field(None, description="Number of limit-up stocks")
    limit_down_count: Optional[int] = Field(None, description="Number of limit-down stocks")
    distribution: List[MarketBreadthBucketResponse] = Field(..., description="Ten percentage-change ranges")
    warnings: List[str] = Field(default_factory=list, description="Fallback or partial-data warnings")

# -----------------------------------------------


def _new_trace_id(ctx: Context | None) -> str:
    """Build a process-unique trace ID even when stateless MCP IDs repeat."""
    mcp_request_id = ctx.request_id if ctx else "direct"
    http_trace_id = http_trace_id_var.get()
    if http_trace_id != "-":
        return f"{http_trace_id}-{mcp_request_id}"
    return f"{mcp_request_id}-{uuid.uuid4().hex[:8]}"


async def fetch_batch_reports(
    symbol_str: str,
    mode: str,
    host: str,
    date: Optional[str] = None,
    fund_flow_limit: int = 15,
    request_id: str = "",
) -> BatchReportResponse:
    """批量获取并生成报告的核心驱动程序"""
    # 1. 预处理：分拆并限流（上限4个）
    raw_symbols = [s.strip().upper() for s in symbol_str.split(',') if s.strip()]
    warnings = []
    if len(raw_symbols) > 4:
        skipped_symbols = raw_symbols[4:]
        warnings.append(
            f"批量查询最多支持 4 个标的；本次仅处理前 4 个，超出的 {len(skipped_symbols)} 个标的不会返回结果："
            f"{','.join(skipped_symbols)}"
        )
        raw_symbols = raw_symbols[:4]
    symbols_label = ",".join(raw_symbols)
    start_time = time.time()
    date_label = f", date={date}" if date else ""
    requirements = FetchRequirements()
    report_cache = get_report_cache()

    def _probe_cache(symbol: str):
        """Return (cache_key, cached_report, started_at). Never raises.

        缓存自身故障只能降级为一次未命中，不能冒泡成整批失败。
        """
        started_at = time.perf_counter()
        cache_key = None
        try:
            cache_key = build_key(
                mode,
                symbol,
                {"date": date, "fund_flow_limit": fund_flow_limit},
                query_date=date,
            )
            cached_report = report_cache.get(cache_key)
        except Exception:
            logger.warning(
                "Report cache lookup failed request_id=%s tool=%s symbol=%s",
                request_id or "-",
                mode,
                symbol,
                exc_info=True,
            )
            cached_report = None
        return cache_key, cached_report, started_at

    def _log_cache_hit(symbol: str, cache_key, cached_report, started_at) -> None:
        logger.info(
            "Report cache hit request_id=%s tool=%s symbol=%s "
            "epoch=%s phase=%s elapsed=%.3fs chars=%s",
            request_id or "-",
            mode,
            symbol,
            cache_key.epoch,
            cache_key.phase,
            time.perf_counter() - started_at,
            len(cached_report),
        )

    # 整批全命中的批次不做任何上游工作，让它去排 BATCH_QUERY_CONCURRENCY 的队会把
    # 一次 0 秒的响应压到慢批次后面——回放显示约 39% 的批次是整批全命中。
    # 这一轮探测只用于"能否跳过准入"的判定：命中就地返回，不经历任何等待；
    # 只要有一个标的未命中就整轮丢弃，由 process_item 在拿到准入之后重新探测。
    # 否则盘中条目会按"排队前"的时刻判定新鲜度，实际陈旧度变成 TTL + 排队时长。
    probes = [(s, _probe_cache(s)) for s in raw_symbols]

    if raw_symbols and all(probe[1] is not None for _, probe in probes):
        output = {
            "symbols_count": len(raw_symbols),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reports": {s: probe[1] for s, probe in probes},
            "errors": {},
            "warnings": warnings,
        }
        with bind_log_context(request_id=request_id or "-", tool=mode):
            for symbol, (cache_key, cached_report, started_at) in probes:
                # 与未命中路径保持同样的关联字段，便于按 symbol 过滤日志
                with bind_log_context(symbol=symbol):
                    _log_cache_hit(symbol, cache_key, cached_report, started_at)
            logger.info(
                "Finished %s query request_id=%s symbols=%s%s cost=%.2fs "
                "cache=all_hit admission=skipped reports=%s response_chars=%s",
                mode,
                request_id or "-",
                symbols_label,
                date_label,
                time.time() - start_time,
                len(output["reports"]),
                sum(len(r) for r in output["reports"].values()),
            )
        return BatchReportResponse(**output)

    admission = _get_batch_query_admission()
    waiting_before = admission.waiting
    active_before = admission.active
    if active_before >= admission.limit:
        logger.info(
            "Batch query queued request_id=%s tool=%s symbols=%s "
            "active=%s waiting=%s limit=%s",
            request_id or "-",
            mode,
            symbols_label,
            active_before,
            waiting_before + 1,
            admission.limit,
        )
    queue_seconds = await admission.acquire()
    service_started_at = time.perf_counter()
    logger.info(
        "Batch query admitted request_id=%s tool=%s symbols=%s "
        "queue=%.3fs active=%s waiting=%s limit=%s",
        request_id or "-",
        mode,
        symbols_label,
        queue_seconds,
        admission.active,
        admission.waiting,
        admission.limit,
    )

    global _active_report_requests
    _active_report_requests += 1
    logger.info(
        "Starting %s query request_id=%s symbols=%s%s active=%s "
        "fetch_finance=%s fetch_fund_flow=%s fetch_realtime=%s fetch_unadjusted=%s",
        mode,
        request_id or "-",
        symbols_label,
        date_label,
        _active_report_requests,
        requirements.finance,
        requirements.fund_flow,
        requirements.realtime,
        requirements.unadjusted_kline,
    )
    
    # 2. 准备容器
    output = {
        "symbols_count": len(raw_symbols),
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reports": {},
        "errors": {},
        "warnings": warnings,
    }
    # 响应外壳的 timestamp 始终重新生成，只有 reports 走缓存

    async def process_item(symbol: str):
        with bind_log_context(request_id=request_id or "-", tool=mode, symbol=symbol):
            symbol_started_at = time.perf_counter()
            # 拿到准入之后重新探测：命中判定必须反映真正开始干活的时刻，
            # 否则盘中条目会带着排队时长一起变旧。命中不得再付出一次 Chromium 抓取。
            cache_key, cached_report, probe_started_at = _probe_cache(symbol)
            if cached_report is not None:
                output["reports"][symbol] = cached_report
                _log_cache_hit(symbol, cache_key, cached_report, probe_started_at)
                return

            # 实时资金流抓取与基础行情并行，避免浏览器排队叠加在数据拉取之后
            prefetch = research.start_realtime_fund_flow_prefetch(symbol, date)
            try:
                # 并行拉取基础行情
                raw_started_at = time.perf_counter()
                raw_data = await research.load_raw_data(
                    symbol,
                    date,
                    host,
                    requirements=requirements,
                )
                raw_elapsed = time.perf_counter() - raw_started_at
                if not raw_data:
                    err_msg = f"未找到证券代码 {symbol} 的相关行情数据。"
                    output["errors"][symbol] = err_msg
                    output["reports"][symbol] = f"Error: {err_msg}"
                    return

                render_started_at = time.perf_counter()
                buf = StringIO()
                # 根据模式按需构建
                research.build_basic_data(buf, symbol, raw_data)
                if mode == "full":
                    await research.build_trading_data(
                        buf,
                        symbol,
                        raw_data,
                        include_historical_fund_flow=True,
                        historical_fund_flow_limit=fund_flow_limit,
                        realtime_fund_flow=prefetch,
                    )
                else:
                    await research.build_trading_data(
                        buf,
                        symbol,
                        raw_data,
                        realtime_fund_flow=prefetch,
                    )

                if mode in ["medium", "full"]:
                    research.build_financial_data(buf, symbol, raw_data)
                if mode == "full":
                    research.build_technical_data(buf, symbol, raw_data)

                output["reports"][symbol] = buf.getvalue()
                fetch_failures = tuple(raw_data.get(FETCH_FAILURES_KEY, ()))
                if (
                    cache_key is not None
                    and not fetch_failures
                    and is_cacheable_report(output["reports"][symbol])
                ):
                    report_cache.put(cache_key, output["reports"][symbol])
                elif fetch_failures:
                    logger.info(
                        "Report cache skipped request_id=%s tool=%s symbol=%s "
                        "incomplete_sources=%s",
                        request_id or "-",
                        mode,
                        symbol,
                        ",".join(fetch_failures),
                    )
                logger.info(
                    "Finished symbol request_id=%s tool=%s symbol=%s "
                    "raw_data=%.3fs render=%.3fs total=%.3fs chars=%s",
                    request_id or "-",
                    mode,
                    symbol,
                    raw_elapsed,
                    time.perf_counter() - render_started_at,
                    time.perf_counter() - symbol_started_at,
                    len(output["reports"][symbol]),
                )
            except Exception as e:
                err_msg = str(e)
                output["errors"][symbol] = err_msg
                output["reports"][symbol] = f"Error during processing: {err_msg}"
                logger.warning(
                    "Failed symbol request_id=%s tool=%s symbol=%s elapsed=%.3fs error=%s",
                    request_id or "-",
                    mode,
                    symbol,
                    time.perf_counter() - symbol_started_at,
                    err_msg,
                )
            finally:
                if prefetch is not None:
                    prefetch.discard()

    # 并发执行所有标的的任务
    try:
        with bind_log_context(request_id=request_id or "-", tool=mode):
            await asyncio.gather(*[process_item(s) for s in raw_symbols])
        return BatchReportResponse(**output)
    finally:
        elapsed = time.time() - start_time
        _active_report_requests -= 1
        response_chars = sum(len(report) for report in output["reports"].values())
        logger.info(
            "Finished %s query request_id=%s symbols=%s%s cost=%.2fs "
            "active=%s reports=%s errors=%s response_chars=%s",
            mode,
            request_id or "-",
            symbols_label,
            date_label,
            elapsed,
            _active_report_requests,
            len(output["reports"]),
            len(output["errors"]),
            response_chars,
        )
        admission.release()
        logger.info(
            "Batch query released request_id=%s tool=%s symbols=%s "
            "queue=%.3fs service=%.3fs total=%.3fs active=%s waiting=%s limit=%s",
            request_id or "-",
            mode,
            symbols_label,
            queue_seconds,
            time.perf_counter() - service_started_at,
            time.time() - start_time,
            admission.active,
            admission.waiting,
            admission.limit,
        )


async def fetch_technical_reports(
    symbol_str: str,
    days: int = 30,
    fields: str = "all",
    include_derived: bool = True,
    date: Optional[str] = None,
    host: str = "",
) -> BatchTechnicalResponse:
    """Fetch machine-readable technical indicator reports."""
    raw_symbols = [s.strip().upper() for s in symbol_str.split(',') if s.strip()]
    warnings = []
    if len(raw_symbols) > 4:
        skipped_symbols = raw_symbols[4:]
        warnings.append(
            f"批量查询最多支持 4 个标的；本次仅处理前 4 个，超出的 {len(skipped_symbols)} 个标的不会返回结果："
            f"{','.join(skipped_symbols)}"
        )
        raw_symbols = raw_symbols[:4]

    symbols_label = ",".join(raw_symbols)
    start_time = time.time()
    date_label = f", date={date}" if date else ""
    logger.info("Starting tech query: %s%s", symbols_label, date_label)

    output = {
        "symbols_count": len(raw_symbols),
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reports": {},
        "errors": {},
        "warnings": warnings,
    }
    report_cache = get_report_cache()

    async def process_item(symbol: str):
        symbol_started_at = time.perf_counter()
        try:
            cache_key = build_key(
                "tech",
                symbol,
                {
                    "days": days,
                    "fields": fields,
                    "include_derived": include_derived,
                    "date": date,
                },
                query_date=date,
            )
            # 缓存故障（含旧版本残留的磁盘条目）只能降级为一次未命中，
            # 不能让整批 gather 失败。
            cached = report_cache.get(cache_key)
            if cached is not None:
                report = TechnicalReport(**cached)
                logger.info(
                    "Report cache hit tool=tech symbol=%s epoch=%s phase=%s "
                    "elapsed=%.3fs indicators=%s",
                    symbol,
                    cache_key.epoch,
                    cache_key.phase,
                    time.perf_counter() - symbol_started_at,
                    len(report.indicators),
                )
                return (report.symbol, report), None

            raw_data = await research.load_raw_data(
                symbol,
                date,
                host,
                requirements=FetchRequirements.technical(),
            )
            if not raw_data:
                return None, (
                    symbol,
                    f"未找到证券代码 {symbol} 的相关行情数据。",
                )

            report_symbol = str(raw_data.get("SYMBOL", symbol))
            indicators = research.get_technical_indicators(
                raw_data,
                days=days,
                fields=fields,
                include_derived=include_derived,
            )
            if not indicators:
                return None, (
                    report_symbol,
                    f"未找到证券代码 {report_symbol} 的技术指标数据。",
                )

            report = TechnicalReport(
                symbol=report_symbol,
                name=str(raw_data.get("NAME", "")),
                quote_date=indicators[0]["date"] if indicators else None,
                indicators=indicators,
            )
            fetch_failures = tuple(raw_data.get(FETCH_FAILURES_KEY, ()))
            if not fetch_failures:
                report_cache.put(cache_key, report.model_dump(mode="json"))
            else:
                logger.info(
                    "Report cache skipped tool=tech symbol=%s incomplete_sources=%s",
                    symbol,
                    ",".join(fetch_failures),
                )
            return (report_symbol, report), None
        except Exception as e:
            return None, (symbol, str(e))

    try:
        results = await asyncio.gather(*[process_item(s) for s in raw_symbols])
        for report, error in results:
            if report:
                output["reports"][report[0]] = report[1]
            if error:
                output["errors"][error[0]] = error[1]
    finally:
        elapsed = time.time() - start_time
        logger.info("Finished tech query: %s%s, cost %.2fs", symbols_label, date_label, elapsed)

    return BatchTechnicalResponse(**output)


class RequestLifecycleLogMiddleware:
  """Log HTTP completion and disconnects around Streamable HTTP requests."""

  def __init__(self, app: ASGIApp):
    self.app = app

  async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
      await self.app(scope, receive, send)
      return

    http_trace_id = uuid.uuid4().hex[:12]
    started_at = time.perf_counter()
    disconnected = False
    response_started = False
    response_finished = False
    status_code = 0
    response_bytes = 0

    async def receive_with_disconnect_log() -> Message:
      nonlocal disconnected
      message = await receive()
      if message["type"] == "http.disconnect" and not disconnected:
        disconnected = True
        if not response_finished:
          # GET 是 Streamable HTTP 的 SSE 通道：它的响应永远不会正常结束，
          # 客户端断开就是其正常终结方式。若一律告警，每个正常的客户端生命
          # 周期都会产生一条 WARNING，真正被放弃的 POST 请求反而被淹没。
          is_event_stream = scope.get("method") == "GET"
          emit = logger.debug if is_event_stream else logger.warning
          emit(
            "HTTP client disconnected before response finished "
            "http_trace_id=%s method=%s path=%s elapsed=%.3fs "
            "response_started=%s response_bytes=%s",
            http_trace_id,
            scope.get("method", ""),
            scope.get("path", ""),
            time.perf_counter() - started_at,
            response_started,
            response_bytes,
          )
      return message

    async def send_with_metrics(message: Message) -> None:
      nonlocal response_started, response_finished, status_code, response_bytes
      await send(message)
      if message["type"] == "http.response.start":
        response_started = True
        status_code = message["status"]
      elif message["type"] == "http.response.body":
        response_bytes += len(message.get("body", b""))
        if not message.get("more_body", False):
          response_finished = True

    outcome = "success"
    try:
      with bind_log_context(http_trace_id=http_trace_id):
        await self.app(scope, receive_with_disconnect_log, send_with_metrics)
      if disconnected and not response_finished:
        outcome = "client_disconnected"
    except BaseException:
      outcome = "error"
      raise
    finally:
      logger.info(
        "HTTP request finished http_trace_id=%s method=%s path=%s status=%s "
        "elapsed=%.3fs response_started=%s response_finished=%s "
        "response_bytes=%s disconnected=%s outcome=%s",
        http_trace_id,
        scope.get("method", ""),
        scope.get("path", ""),
        status_code,
        time.perf_counter() - started_at,
        response_started,
        response_finished,
        response_bytes,
        disconnected,
        outcome,
      )


class QtfMCP(FastMCP):

  def streamable_http_app(self) -> Starlette:
    super_app = super().streamable_http_app()
    super_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    super_app.add_middleware(RequestLifecycleLogMiddleware)
    return super_app

# Create an MCP server
mcp_app = QtfMCP(
  "CnStock",
  sse_path="/cnstock/sse",
  message_path="/cnstock/messages/",
  streamable_http_path="/cnstock/mcp",
  stateless_http=True,
)
@mcp_app.tool()
async def brief(
  symbol: str,
  date: Optional[str] = None,
  fund_flow_limit: Optional[int] = None,
  ctx: Context = None,
) -> BatchReportResponse:  # type: ignore
  """Get brief information and fund flow for input stock symbol(s) (Batch Supported).
  Includes:
  - basic data
  - trading data (including real-time fund flow)
  
  Args:
    symbol (str): Stock symbol or comma-separated list (up to 4), e.g., "SZ300308,SH000001"
    date (str, optional): Query cutoff date in YYYY-MM-DD format. Defaults to latest available trading day.
    fund_flow_limit (int, optional): Ignored; only the full tool uses this parameter.

  Returns:
    A BatchReportResponse object containing multiple reports or errors.
  """
  who = ctx.request_context.request.client.host if ctx else ""  # type: ignore
  return await fetch_batch_reports(
    symbol,
    "brief",
    who,
    date,
    request_id=_new_trace_id(ctx),
  )


@mcp_app.tool()
async def medium(
  symbol: str,
  date: Optional[str] = None,
  fund_flow_limit: Optional[int] = None,
  ctx: Context = None,
) -> BatchReportResponse:  # type: ignore
  """Get medium information for input stock symbol(s) (Batch Supported).
  Includes:
  - basic data
  - trading data (including real-time fund flow)
  - financial data (abstract)

  Args:
    symbol (str): Stock symbol or comma-separated list (up to 4), e.g., "SZ300308,SH000001"
    date (str, optional): Query cutoff date in YYYY-MM-DD format. Defaults to latest available trading day.
    fund_flow_limit (int, optional): Ignored; only the full tool uses this parameter.

  Returns:
    A BatchReportResponse object containing multiple reports or errors.
  """
  who = ctx.request_context.request.client.host if ctx else ""  # type: ignore
  return await fetch_batch_reports(
    symbol,
    "medium",
    who,
    date,
    request_id=_new_trace_id(ctx),
  )


@mcp_app.tool()
async def full(
  symbol: str,
  date: Optional[str] = None,
  fund_flow_limit: int = 15,
  ctx: Context = None,
) -> BatchReportResponse:  # type: ignore
  """Get full information for input stock symbol(s) (Batch Supported).
  Includes:
  - basic data
  - trading data (including real-time fund flow)
  - financial data (comprehensive)
  - technical analysis data (MACD, KDJ, etc.)

  Args:
    symbol (str): Stock symbol or comma-separated list (up to 4), e.g., "SZ300308,SH000001"
    date (str, optional): Query cutoff date in YYYY-MM-DD format. Defaults to latest available trading day.
    fund_flow_limit (int, optional): Number of historical fund-flow rows to show. Defaults to 15.

  Returns:
    A BatchReportResponse object containing multiple reports or errors.
  """
  who = ctx.request_context.request.client.host if ctx else ""  # type: ignore
  return await fetch_batch_reports(
    symbol,
    "full",
    who,
    date,
    fund_flow_limit=fund_flow_limit,
    request_id=_new_trace_id(ctx),
  )


@mcp_app.tool()
async def tech(
  symbol: str,
  days: int = 30,
  fields: str = "all",
  include_derived: bool = True,
  date: Optional[str] = None,
  ctx: Context = None,  # type: ignore
) -> BatchTechnicalResponse:
  """Get machine-readable technical indicators for input stock symbol(s).
  Returns strict JSON objects instead of Markdown. Supports batch mode.

  Args:
    symbol (str): Stock symbol or comma-separated list (up to 4), e.g., "SZ002463,SH688981".
    days (int): Recent N trading days to return. Default is 30.
    fields (str): Indicator groups to include: all, or comma-separated values from macd,kdj,rsi,bbands.
    include_derived (bool): Include macd.histogram = dif - dea when true.
    date (str, optional): Query cutoff date in YYYY-MM-DD format. Defaults to latest available trading day.

  Returns:
    A BatchTechnicalResponse object containing JSON technical reports or errors.
  """
  who = ctx.request_context.request.client.host if ctx else ""  # type: ignore
  return await fetch_technical_reports(symbol, days, fields, include_derived, date, who)


@mcp_app.tool()
async def kline_daily(
  symbol: str,
  date: str,
  adjust: Literal["qfq", "hfq", "none"] = "qfq",
  ctx: Context = None,  # type: ignore
) -> str:
  """获取指定日期的股票日K线数据
  
  Get daily K-line data for a specific date.
  
  Args:
    symbol (str): 股票代码，格式如 "SH600000" 或 "SZ000001"。
                  Stock symbol, must be in the format of "SH600000" or "SZ000001".
    date (str): 查询日期，格式 "YYYY-MM-DD"，如 "2024-12-13"。
                Query date in format "YYYY-MM-DD".
    adjust (str): 复权类型。"qfq"=前复权(默认), "hfq"=后复权, "none"=不复权。
                  Adjustment type: "qfq"=forward adjust(default), "hfq"=backward adjust, "none"=no adjust.
  
  Returns:
    该日期的K线数据，包含开盘价、收盘价、最高价、最低价、成交量、成交额等。
  """
  datasource = get_datasource()
  report_cache = get_report_cache()
  started_at = time.perf_counter()
  cache_key = build_key(
    "kline_daily",
    symbol,
    {"date": date, "adjust": adjust},
    query_date=date,
  )
  cached = report_cache.get(cache_key)
  if cached is not None:
    logger.info(
      "Report cache hit tool=kline_daily symbol=%s epoch=%s phase=%s "
      "elapsed=%.3fs chars=%s",
      symbol,
      cache_key.epoch,
      cache_key.phase,
      time.perf_counter() - started_at,
      len(cached),
    )
    return cached

  result = await datasource.fetch_kline_simple(symbol, date, date, adjust)

  if result is None or not result.get("data"):
    logger.info(
      "Finished kline_daily symbol=%s date=%s adjust=%s cache=miss "
      "elapsed=%.3fs outcome=empty",
      symbol,
      date,
      adjust,
      time.perf_counter() - started_at,
    )
    return f"未找到 {symbol} 在 {date} 的数据。可能是非交易日或股票代码有误。"
  
  data = result["data"][0]
  adjust_name = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}.get(adjust, adjust)
  
  buf = StringIO()
  print(f"# {symbol} {date} 日K线数据 ({adjust_name})", file=buf)
  print("", file=buf)
  print(f"- 开盘价: {data['开盘']:.2f}", file=buf)
  print(f"- 收盘价: {data['收盘']:.2f}", file=buf)
  print(f"- 最高价: {data['最高']:.2f}", file=buf)
  print(f"- 最低价: {data['最低']:.2f}", file=buf)
  print(f"- 成交量: {data['成交量']:,}", file=buf)
  print(f"- 成交额: {data['成交额']:,.2f}", file=buf)
  print(f"- 涨跌幅: {data['涨跌幅']:.2f}%", file=buf)
  print(f"- 涨跌额: {data['涨跌额']:.2f}", file=buf)
  print(f"- 振幅: {data['振幅']:.2f}%", file=buf)
  print(f"- 换手率: {data['换手率']:.2f}%", file=buf)

  report = buf.getvalue()
  report_cache.put(cache_key, report)
  logger.info(
    "Finished kline_daily symbol=%s date=%s adjust=%s cache=miss "
    "elapsed=%.3fs chars=%s",
    symbol,
    date,
    adjust,
    time.perf_counter() - started_at,
    len(report),
  )
  return report


@mcp_app.tool()
async def kline_range(
  symbol: str,
  start_date: str,
  end_date: str,
  adjust: Literal["qfq", "hfq", "none"] = "qfq",
  ctx: Context = None,  # type: ignore
) -> str:
  """获取指定日期区间的股票日K线数据
  
  Get daily K-line data for a date range.
  
  Args:
    symbol (str): 股票代码，格式如 "SH600000" 或 "SZ000001"。
                  Stock symbol, must be in the format of "SH600000" or "SZ000001".
    start_date (str): 开始日期，格式 "YYYY-MM-DD"，如 "2024-12-01"。
                      Start date in format "YYYY-MM-DD".
    end_date (str): 结束日期，格式 "YYYY-MM-DD"，如 "2024-12-13"。
                    End date in format "YYYY-MM-DD".
    adjust (str): 复权类型。"qfq"=前复权(默认), "hfq"=后复权, "none"=不复权。
                  Adjustment type: "qfq"=forward adjust(default), "hfq"=backward adjust, "none"=no adjust.
  
  Returns:
    日期区间内的K线数据表格，包含每日的开高低收、成交量、涨跌幅等。
  """
  datasource = get_datasource()
  report_cache = get_report_cache()
  started_at = time.perf_counter()
  cache_key = build_key(
    "kline_range",
    symbol,
    {"start_date": start_date, "end_date": end_date, "adjust": adjust},
    query_date=end_date,
  )
  cached = report_cache.get(cache_key)
  if cached is not None:
    logger.info(
      "Report cache hit tool=kline_range symbol=%s epoch=%s phase=%s "
      "elapsed=%.3fs chars=%s",
      symbol,
      cache_key.epoch,
      cache_key.phase,
      time.perf_counter() - started_at,
      len(cached),
    )
    return cached

  result = await datasource.fetch_kline_simple(symbol, start_date, end_date, adjust)

  if result is None or not result.get("data"):
    logger.info(
      "Finished kline_range symbol=%s range=%s~%s adjust=%s cache=miss "
      "elapsed=%.3fs outcome=empty",
      symbol,
      start_date,
      end_date,
      adjust,
      time.perf_counter() - started_at,
    )
    return f"未找到 {symbol} 在 {start_date} 至 {end_date} 期间的数据。"
  
  data_list = result["data"]
  adjust_name = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}.get(adjust, adjust)
  
  buf = StringIO()
  print(f"# {symbol} K线数据 ({start_date} 至 {end_date}, {adjust_name})", file=buf)
  print("", file=buf)
  print(f"共 {len(data_list)} 个交易日", file=buf)
  print("", file=buf)
  
  # 表格头
  print("| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 涨跌幅 |", file=buf)
  print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |", file=buf)
  
  # 表格内容
  for item in data_list:
    print(
      f"| {item['日期']} | {item['开盘']:.2f} | {item['收盘']:.2f} | "
      f"{item['最高']:.2f} | {item['最低']:.2f} | {item['成交量']:,} | "
      f"{item['涨跌幅']:.2f}% |",
      file=buf
    )

  report = buf.getvalue()
  report_cache.put(cache_key, report)
  logger.info(
    "Finished kline_range symbol=%s range=%s~%s adjust=%s cache=miss "
    "elapsed=%.3fs rows=%s chars=%s",
    symbol,
    start_date,
    end_date,
    adjust,
    time.perf_counter() - started_at,
    len(data_list),
    len(report),
  )
  return report


@mcp_app.tool()
async def market_breadth(ctx: Context = None) -> MarketBreadthResponse:  # type: ignore
  """获取全 A 股上涨、下跌、平盘、涨跌停家数和涨跌幅分布。

  Get whole-market A-share breadth, including rise/fall/flat counts,
  limit-up/limit-down counts, and ten percentage-change ranges.

  Returns:
    A structured MarketBreadthResponse. The source field identifies the
    provider used; warnings describe fallback or partial data.
  """
  data = await get_market_breadth()
  return MarketBreadthResponse(
    source=data.source,
    fetched_at=data.fetched_at,
    trade_date=data.trade_date,
    market_time=data.market_time,
    up_count=data.up_count,
    down_count=data.down_count,
    flat_count=data.flat_count,
    limit_up_count=data.limit_up_count,
    limit_down_count=data.limit_down_count,
    distribution=[
      MarketBreadthBucketResponse(range=bucket.label, count=bucket.count)
      for bucket in data.distribution
    ],
    warnings=list(data.warnings),
  )
