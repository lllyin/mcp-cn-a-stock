"""
CN Stock 数据源实现

综合使用 AkShare 和 efinance 获取 A 股行情数据。
"""

import asyncio
import collections
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np

from ..config import (
    AKSHARE_PROXY_ENABLED,
    AKSHARE_PROXY_IP,
    AKSHARE_PROXY_PASSWORD,
    AKSHARE_PROXY_RETRY,
    FETCH_MAX_IN_FLIGHT,
    FETCH_MAX_WORKERS,
    FINANCE_CACHE_MAX_ENTRIES,
    FINANCE_CACHE_TTL_SECONDS,
    FUND_FLOW_PAGE_CONCURRENCY,
    FUND_FLOW_PAGE_COOLDOWN_SECONDS,
    FUND_FLOW_PAGE_ENABLED,
    FUND_FLOW_PAGE_FAILURE_WINDOW_SECONDS,
    FUND_FLOW_PAGE_OPEN_AFTER_FAILURES,
    FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS,
    FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS,
    SH_INDICES,
    SOURCE_BREAKER_COOLDOWN_SECONDS,
    SOURCE_BREAKER_ENABLED,
    SOURCE_BREAKER_OPEN_AFTER_FAILURES,
    SZ_INDICES,
)
from . import basic_info
from .base import DataSource, FetchRequirements, StockData
from .http_channel import (
    impersonated_hosts_degraded,
    install_http_channel,
    installed_mode,
)
from ..observability import bind_log_context, log_context

logger = logging.getLogger("qtf_mcp")

_FETCH_FAILURE_MARKER = "_fetch_failure"


def _fetch_failure(source: str) -> Dict[str, str]:
    """Return an internal sentinel for a source that failed or was unavailable."""
    return {_FETCH_FAILURE_MARKER: source}


def _is_fetch_failure(result) -> bool:
    """Whether a fetch result carries the failure sentinel."""
    return isinstance(result, dict) and _FETCH_FAILURE_MARKER in result


class SourceBreaker:
    """Skip an upstream source that is provably refusing, with half-open probing.

    Scoped to a provider step rather than a host or a URL: Eastmoney refuses per
    endpoint (push2his serves its root while refusing /api/qt/stock/kline/get),
    and the HTTP-channel hook only exists in impersonate mode, so keying lower
    would make behaviour depend on the channel.

    Once open, every request skips the source except one probe per cooldown, so
    the cooldown bounds recovery latency instead of the cost of staying open.

    失败可以按两种口径计数，由 ``window`` 选择：

    ``window == 0``（默认）
        **连续**失败，一次成功清零。适合"要么全通要么全封"的来源。

    ``window > 0``
        **滑动窗口内**累计失败，成功不清零，只靠时间过期。适合"逐次随机被拒"的
        来源——连续计数在持续 50% 拒绝率下几乎永远开不了，因为总有一次成功把它
        清零，于是每次请求都白付一次代价。资金流向页面就是这一类，见配置项里的
        实测数据。

    ``degraded`` 是一个可选判据：返回 True 就直接跳过这个源，不看失败计数。用于
    "这一刻已经知道必败"的情形，省掉用失败去重新发现它的那一段。判据由调用方注入
    而不是写死，源这一层不该知道出站通道是怎么实现的。
    """

    def __init__(
        self,
        name: str,
        threshold: int,
        cooldown: float,
        window: float = 0.0,
        degraded=None,
    ):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self.window = window
        self.degraded = degraded
        self._lock = threading.Lock()
        self._failures = 0
        self._failed_at: collections.deque = collections.deque()
        self._open_until = 0.0
        self._probing = False

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open_until > 0.0

    def should_skip(self) -> bool:
        """Whether to bypass the source. Grants exactly one probe per cooldown."""
        if not SOURCE_BREAKER_ENABLED:
            return False
        if self.degraded is not None:
            try:
                if self.degraded():
                    # 已知必败，连半开探测都不放：探测也要走同一条降级的通道。
                    return True
            except Exception:
                # 判据自己坏了不能拖垮取数——退回按失败计数走。
                logger.debug("熔断器降级判据异常 source=%s", self.name, exc_info=True)
        with self._lock:
            if self._open_until <= 0.0:
                return False
            if time.monotonic() < self._open_until or self._probing:
                return True
            self._probing = True
            return False

    def record(self, *, success: bool, cooldown: Optional[float] = None) -> None:
        """Account for an attempt that actually reached the source.

        ``cooldown`` overrides the configured value for this outcome only, for
        failures that are known to need a longer back-off than an ordinary one.
        """
        if not SOURCE_BREAKER_ENABLED:
            return
        effective_cooldown = self.cooldown if cooldown is None else cooldown
        with self._lock:
            reopened = False
            recovered = False
            if success:
                recovered = self._open_until > 0.0
                # 窗口口径下成功不清零：清零就退化成连续计数，而窗口存在的理由
                # 正是"逐次随机被拒时，成功和失败是交替出现的"。让时间去过期它。
                if not self.window:
                    self._failures = 0
                self._open_until = 0.0
                self._probing = False
            elif self._open_until > 0.0:
                # A failed probe buys another cooldown rather than a new streak.
                self._open_until = time.monotonic() + effective_cooldown
                self._probing = False
            elif self.window:
                now = time.monotonic()
                self._failed_at.append(now)
                while self._failed_at and now - self._failed_at[0] > self.window:
                    self._failed_at.popleft()
                if len(self._failed_at) >= self.threshold:
                    self._failed_at.clear()
                    self._open_until = now + effective_cooldown
                    reopened = True
            else:
                self._failures += 1
                if self._failures >= self.threshold:
                    self._failures = 0
                    self._open_until = time.monotonic() + effective_cooldown
                    reopened = True

        if recovered:
            logger.info("Source breaker closed source=%s", self.name)
        elif reopened:
            request_id, tool, symbol = log_context()
            logger.warning(
                "Source breaker opened source=%s channel=%s cooldown=%ss "
                "request_id=%s tool=%s symbol=%s; using the fallback source",
                self.name,
                installed_mode(),
                effective_cooldown,
                request_id,
                tool,
                symbol,
            )

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._failed_at.clear()
            self._open_until = 0.0
            self._probing = False


# The K-line tier gets a breaker because it has an equivalent fallback; the
# HTTP fund-flow tier does not, since skipping it would return the same empty
# result without buying anything.
#
# 带上降级判据：这个源打的是 push2his.eastmoney.com，正是 IMPERSONATED_HOSTS 之一。
# 伪装通道一进冷却，它的请求就退回原生 requests，而那台主机被列进名单的理由就是
# 它拒绝原生 requests——冷却期内每次尝试都是已知必败。2026-09-05 部署机实测这段
# 空转的代价：8 次 K 线调用各 12.9~14.7s，占该窗口 K 线总耗时的 87%。
_KLINE_BREAKER = SourceBreaker(
    "eastmoney_kline",
    SOURCE_BREAKER_OPEN_AFTER_FAILURES,
    SOURCE_BREAKER_COOLDOWN_SECONDS,
    degraded=impersonated_hosts_degraded,
)

# The page fallback does get one, for the opposite reason: it is expensive
# rather than cheap. The page fills its table from the same endpoint the HTTP
# tier uses, so once that endpoint refuses, every attempt is futile and costs a
# Chromium page load. Measured on 2026-09-03: one futile attempt turned a 6.7s
# request into 20.1s.
#
# 用窗口口径而不是连续口径：实测被拒是逐次随机的（单次被拒率 12.5%，而同一批
# 标的可获取 94%），连续计数在这种上游下两头都不准——两次噪声就能凑满阈值把整层
# 停掉，而真的持续半通时又总有一次成功把它清零。
_FUND_FLOW_PAGE_BREAKER = SourceBreaker(
    "fund_flow_page",
    FUND_FLOW_PAGE_OPEN_AFTER_FAILURES,
    FUND_FLOW_PAGE_COOLDOWN_SECONDS,
    window=FUND_FLOW_PAGE_FAILURE_WINDOW_SECONDS,
)


# 兜底 K 线的帧归一逻辑搬到了 kline_frame，好让 provider 直接用而不产生循环依赖。
# 这些名字仍从本模块可见：外部按 cn_stock_source.<name> 取的地方不用改。
from . import kline_source
from .kline_frame import (  # noqa: F401
    FALLBACK_FRAME_COLUMNS,
    _FALLBACK_REQUIRED,
    _INDEX_CODE_PREFIXES,
    _UNSUPPORTED_ERRORS,
    _as_date,
    _finalize_fallback_frame,
    _is_index_code,
    _market_prefixed_symbol,
    _normalize_volume_to_lots,
    append_intraday_bar,
)

def check_is_index(symbol: str, name: str) -> bool:
    """判定是否为指数的辅助函数"""
    if not symbol:
        return False
    
    market = symbol[:2].upper()
    code = symbol[2:]
    
    # 1. 优先根据配置中的显式名单判定
    if market == "SH" and code in SH_INDICES:
        return True
    if market == "SZ" and code in SZ_INDICES:
        return True
        
    # 2. 备选方案：通过名称猜测 (增加类型校验防止 float 类型报错)
    if name and isinstance(name, str) and ("指数" in name or "Index" in name):
        return True
    
    # 特别前缀处理
    if symbol.startswith(("SZ39", "SH93")):
        return True
    return False

# Exactly one outbound HTTP channel, and it must be installed before efinance is
# imported; see http_channel for why the import order is load bearing.
install_http_channel(
    proxy_enabled=AKSHARE_PROXY_ENABLED,
    proxy_gateway=AKSHARE_PROXY_IP,
    proxy_token=AKSHARE_PROXY_PASSWORD,
    proxy_retry=AKSHARE_PROXY_RETRY,
)

import efinance as ef

# 线程池用于执行同步的调用
_executor = ThreadPoolExecutor(
    max_workers=FETCH_MAX_WORKERS,
    thread_name_prefix="cn-stock-data",
)
_DATA_FETCH_SLOTS_ATTR = "_cn_stock_data_fetch_slots"
_FINANCE_INFLIGHT_ATTR = "_cn_stock_finance_inflight"
_FUND_FLOW_PAGE_SLOTS_ATTR = "_cn_stock_fund_flow_page_slots"
_FUND_FLOW_PAGE_BUDGET_ATTR = "_cn_stock_fund_flow_page_budgets"
# 同时记住多少个请求的预算。请求预算只活 8 秒，正常情况下这张表里只有几条；
# 上限是防"日志上下文缺失导致 key 退化"之类的意外把它撑爆。
_FUND_FLOW_PAGE_BUDGET_MAX_ENTRIES = 256
# 预算到期后还要把记录留多久。见 _fund_flow_page_wait_budget 里的说明：一到期
# 就删等于允许无限续期。取 60 秒——远长于一次请求，又不至于让表堆积。
_FUND_FLOW_PAGE_BUDGET_GRACE_SECONDS = 60.0
_finance_cache: dict[str, tuple[float, Dict]] = {}
_finance_cache_lock = threading.Lock()


def _get_data_fetch_slots() -> asyncio.Semaphore:
    """Return a limiter owned by the current event loop."""
    loop = asyncio.get_running_loop()
    slots = getattr(loop, _DATA_FETCH_SLOTS_ATTR, None)
    if slots is None:
        slots = asyncio.Semaphore(FETCH_MAX_IN_FLIGHT)
        setattr(loop, _DATA_FETCH_SLOTS_ATTR, slots)
    return slots


def _get_fund_flow_page_slots() -> asyncio.Semaphore:
    """Return the page-fallback limiter owned by the current event loop."""
    loop = asyncio.get_running_loop()
    slots = getattr(loop, _FUND_FLOW_PAGE_SLOTS_ATTR, None)
    if slots is None:
        slots = asyncio.Semaphore(FUND_FLOW_PAGE_CONCURRENCY)
        setattr(loop, _FUND_FLOW_PAGE_SLOTS_ATTR, slots)
    return slots


def _fund_flow_page_wait_budget(request_id: str) -> float:
    """这个标的还能为等名额花多少秒。

    要解决的是"同一个请求里的标的在互相抢名额"：``mcp_app`` 对 raw_symbols 是
    ``asyncio.gather`` 全并发，每个标的各自独立地去抢那几个名额，输给自己兄弟的
    那几个在碰到上游之前就被判了缺数据。按标的计时表达不了"这一批整体值得等多久"，
    所以预算按 ``request_id`` 归集，第一个标的进来时开始计时。

    返回值已经和单标的上限 ``FALLBACK_WAIT_SECONDS`` 取过小：前者管整批，后者管
    单个，两个都置 0 就退回"不等，直接跳过"的老行为。
    """
    per_symbol = FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS
    if FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS <= 0:
        return per_symbol

    loop = asyncio.get_running_loop()
    budgets = getattr(loop, _FUND_FLOW_PAGE_BUDGET_ATTR, None)
    if budgets is None:
        budgets = {}
        setattr(loop, _FUND_FLOW_PAGE_BUDGET_ATTR, budgets)

    now = time.monotonic()
    # 就地剪枝，和 _page_cache 一个模式：不引入后台任务，也不让表无界增长。
    #
    # 必须留一段宽限期，不能一到期就删：到期正是"这一批预算已经用尽"的状态，
    # 删掉它下一个标的就会拿到一份全新的预算，于是 40 个标的的批次可以无限续期，
    # 预算这层等于不存在。宽限期要长于一次请求的合理时长。
    horizon = now - _FUND_FLOW_PAGE_BUDGET_GRACE_SECONDS
    for stale in [k for k, deadline in budgets.items() if deadline < horizon]:
        budgets.pop(stale, None)
    if len(budgets) >= _FUND_FLOW_PAGE_BUDGET_MAX_ENTRIES:
        budgets.pop(next(iter(budgets)), None)

    # request_id 缺失时（直接调用、测试）退化成每个标的独享一份预算，而不是让
    # 所有调用共享同一个 key —— 那会让互不相关的调用互相扣预算。
    if not request_id or request_id == "-":
        return per_symbol

    deadline = budgets.get(request_id)
    if deadline is None:
        deadline = now + FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS
        budgets[request_id] = deadline
    return min(per_symbol, max(0.0, deadline - now))


def _release_data_fetch_slot(slots: asyncio.Semaphore, future: asyncio.Future) -> None:
    slots.release()
    if not future.cancelled():
        future.exception()


def _get_finance_inflight() -> dict[str, asyncio.Task[Optional[Dict]]]:
    """Return the current event loop's finance singleflight registry."""
    loop = asyncio.get_running_loop()
    inflight = getattr(loop, _FINANCE_INFLIGHT_ATTR, None)
    if inflight is None:
        inflight = {}
        setattr(loop, _FINANCE_INFLIGHT_ATTR, inflight)
    return inflight


def _complete_finance_inflight(
    inflight: dict[str, asyncio.Task[Optional[Dict]]],
    cache_key: str,
    task: asyncio.Task[Optional[Dict]],
) -> None:
    if inflight.get(cache_key) is task:
        inflight.pop(cache_key, None)
    if not task.cancelled():
        task.exception()


def _prune_finance_cache(now: float, *, reserve_entry: bool = False) -> tuple[int, int]:
    """Remove expired and oldest finance entries while holding the cache lock."""
    expired_codes = [
        code
        for code, (cached_at, _) in _finance_cache.items()
        if now - cached_at > FINANCE_CACHE_TTL_SECONDS
    ]
    for code in expired_codes:
        _finance_cache.pop(code, None)

    evicted = 0
    target_size = FINANCE_CACHE_MAX_ENTRIES - 1 if reserve_entry else FINANCE_CACHE_MAX_ENTRIES
    while len(_finance_cache) > target_size:
        oldest_code = min(_finance_cache, key=lambda code: _finance_cache[code][0])
        _finance_cache.pop(oldest_code, None)
        evicted += 1
    return len(expired_codes), evicted


def _execute_timed(func, args, requested_at, submitted_at, request_id, tool, symbol):
    started_at = time.perf_counter()
    try:
        # 线程池的 worker 线程有自己的一份 contextvars，默认全是 "-"。不在这里补绑
        # 一次的话，同步函数内部打出来的日志——K 线失败、兜底、熔断打开——全都没有
        # request_id，而它们恰恰是排查时最需要和入口那条串起来的几行。
        with bind_log_context(request_id=request_id, tool=tool, symbol=symbol):
            return func(*args)
    finally:
        logger.debug(
            "Data task %s request_id=%s tool=%s symbol=%s "
            "admission=%.3fs queue=%.3fs service=%.3fs",
            func.__name__,
            request_id,
            tool,
            symbol,
            submitted_at - requested_at,
            started_at - submitted_at,
            time.perf_counter() - started_at,
        )


async def _run_in_executor(func, *args):
    """在有界线程池中运行同步函数。"""
    requested_at = time.perf_counter()
    slots = _get_data_fetch_slots()
    await slots.acquire()
    submitted_at = time.perf_counter()
    request_id, tool, symbol = log_context()
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(
            _executor,
            _execute_timed,
            func,
            args,
            requested_at,
            submitted_at,
            request_id,
            tool,
            symbol,
        )
    except BaseException:
        slots.release()
        raise

    # A cancelled MCP request cannot stop a synchronous network call that is
    # already running. Keep its permit until the executor future truly ends.
    future.add_done_callback(
        lambda completed: _release_data_fetch_slot(slots, completed)
    )
    return await asyncio.shield(future)


class CNStockDataSource(DataSource):
    """
    CN Stock 数据源
    
    综合使用 AkShare 和 efinance 获取 A 股数据，包括：
    - 日K线数据（支持复权）
    - 财务数据
    - 资金流向
    - 分红数据
    - 实时估值指标 (PE/PB)
    """
    
    @property
    def name(self) -> str:
        return "CNStock"
    
    def _safe_float(self, value, default: float = 0.0) -> float:
        """
        安全将任意值转换为 float。
        对于 ETF、停牌股等特殊品种，efinance 可能返回 '-' 、None 等非数字内容。
        """
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        if s in ("", "-", "--", "N/A", "nan"):
            return default
        try:
            return float(s)
        except (ValueError, TypeError):
            return default
    
    def _symbol_to_akshare(self, symbol: str) -> tuple[str, str]:
        """
        将内部格式转换为 akshare 格式，并增加自动纠偏逻辑
        
        SH000333 -> ("000333", "sz") # 自动识别归属
        """
        code = "".join([c for c in symbol if c.isdigit()])
        if not code:
            return "", "sh"
            
        # 智能识别归属：优先根据代码开头的特征判断
        # 600/601/603/605/688 -> SH
        if code.startswith(("60", "68", "90")):
            market = "sh"
        # 000/001/002/300/301 -> SZ (除非是 SH000xxx 且在指数列表里)
        elif code.startswith(("00", "20", "30")):
            # 对 000 段位进行细分：个股 vs 指数
            if symbol.upper().startswith("SH") and code.startswith("000"):
                # 检查是否在沪市核心指数名单中 (从 confs/indices.json 加载)
                if code in SH_INDICES:
                    market = "sh"
                else:
                    # 如果不是知名指数，即便写了 SH，也纠正为 SZ（如 SH000333 -> SZ000333）
                    market = "sz"
            else:
                market = "sz"
        elif code.startswith(("1", "5")): # 基金/ETF
            if code.startswith("5"): market = "sh"
            else: market = "sz"
        # 北交所：43/83/87/88 段与新号段 92。原来没有这一条，它们落进下面的兜底
        # 被判成 sz，于是腾讯被问 sz430047、新浪同理，两家都 KeyError，三级 K 线
        # 全挂，整只票返回"未找到相关行情数据"——而腾讯本身是支持 bj430047 的。
        elif code.startswith(("43", "83", "87", "88", "92")):
            market = "bj"
        else:
            # 兜底：保留用户指定的前缀
            market = "sh" if symbol.upper().startswith("SH") else "sz"
            
        return code, market

    def _get_canonical_symbol(self, code: str, market: str) -> str:
        """返回规范化的代码格式"""
        return f"{market.upper()}{code}"
    
    def _akshare_to_symbol(self, code: str, market: str = "") -> str:
        """
        将 akshare 格式转换为内部格式
        
        ("600000", "sh") -> "SH600000"
        """
        if market:
            lowered = market.lower()
            if lowered in ("sh", "1"):
                prefix = "SH"
            elif lowered == "bj":
                prefix = "BJ"
            else:
                prefix = "SZ"
        else:
            prefix = "SH" if code.startswith("6") else "SZ"
        return f"{prefix}{code}"
    
    def _date_to_ns(self, date_val) -> int:
        """将日期转换为纳秒时间戳"""
        if isinstance(date_val, str):
            dt = datetime.strptime(date_val[:10], "%Y-%m-%d")
        elif hasattr(date_val, "timestamp"):
            dt = date_val
        else:
            dt = datetime.strptime(str(date_val)[:10], "%Y-%m-%d")
        return int(dt.timestamp() * 1e9)
    
    def _parse_numeric_column(self, series, is_percent: bool = False) -> np.ndarray:
        """
        解析数值列，处理各种格式
        
        Args:
            series: pandas Series
            is_percent: 是否为百分比格式（如 "24.00%"）
        """
        def parse_value(val):
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return 0.0
            if isinstance(val, (int, float)):
                return float(val)
            
            # 字符串处理
            s = str(val).strip()
            if s == "" or s == "-" or s == "--":
                return 0.0
            
            # 移除百分号
            if s.endswith("%"):
                s = s[:-1]
                try:
                    return float(s) / 100.0
                except ValueError:
                    return 0.0
            
            # 处理亿/万单位
            multiplier = 1.0
            if s.endswith("亿"):
                s = s[:-1]
                multiplier = 1e8
            elif s.endswith("万"):
                s = s[:-1]
                multiplier = 1e4
            
            try:
                return float(s) * multiplier
            except ValueError:
                return 0.0
        
        result = np.array([parse_value(v) for v in series], dtype=np.float64)
        return result
    
    def _fetch_kline_sync(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
        symbol: str = None,
        include_unadjusted: bool = True,
        status: dict = None,
    ) -> Optional[Dict]:
        """同步获取K线数据"""
        if _KLINE_BREAKER.should_skip():
            # 东财这一级正在熔断，直接用兜底源，省掉必然失败的整条重试链。
            return self._fallback_kline_result(
                code, start_date, end_date, adjust, symbol, include_unadjusted, status
            )

        eastmoney_ok = True
        try:
            from ..symbols import get_symbol_name
            symbol_name = get_symbol_name(symbol) if symbol else ""
            is_index = check_is_index(symbol, symbol_name)
            # 对指数优先使用名称查询 (解决科创50等代码冲突问题)
            query_code = symbol_name if (is_index and symbol_name) else (symbol if is_index else code)

            # 映射复权类型
            adj_map = {"qfq": 1, "hfq": 2, "none": 0}
            fqt = adj_map.get(adjust, 1)
            
            df = None
            # 特殊处理：如果是新上市的 ETF (以 1 或 5 开头)，efinance 往往识别不了
            # 我们直接用 akshare 获取，避免 efinance 的 8s 超时等待
            if code.startswith(("1", "5")):
                import akshare as ak
                ak_adj_map = {1: "qfq", 2: "hfq", 0: ""}
                ak_adj = ak_adj_map.get(fqt, "qfq")
                df = ak.fund_etf_hist_em(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust=ak_adj
                )
            
            if df is None or df.empty:
                # 使用 efinance 获取日K线
                df = ef.stock.get_quote_history(
                    query_code,
                    beg=start_date.replace("-", ""),
                    end=end_date.replace("-", ""),
                    fqt=fqt
                )
            
            if df is None or df.empty:
                # 如果 efinance 还是不行 (可能由于代码不属于1/5开头但也是新股)，尝试 fallback
                logger.warning(f"efinance 获取K线数据为空 {code}，尝试使用 akshare fallback...")
                import akshare as ak
                ak_adj_map = {1: "qfq", 2: "hfq", 0: ""}
                ak_adj = ak_adj_map.get(fqt, "qfq")
                
                # 判断是股票还是基金
                if code.startswith(("1", "5")):
                    df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust=ak_adj)
                elif is_index:
                    df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
                else:
                    df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust=ak_adj)
                
                if df is None or df.empty:
                    logger.warning(f"获取K线数据依然为空 {code}，尝试腾讯历史行情 fallback...")
                    eastmoney_ok = False
                    df = self._fetch_fallback_kline_sync(
                        code, start_date, end_date, adjust, symbol, status
                    )
                    if df is None or df.empty:
                        return None
            
            # 同时获取不复权数据用于计算
            if fqt != 0 and include_unadjusted:
                df_unadj = None
                if code.startswith(("1", "5")):
                    import akshare as ak
                    df_unadj = ak.fund_etf_hist_em(
                        symbol=code,
                        period="daily",
                        start_date=start_date.replace("-", ""),
                        end_date=end_date.replace("-", ""),
                        adjust=""
                    )
                
                if df_unadj is None or df_unadj.empty:
                    df_unadj = ef.stock.get_quote_history(
                        query_code,
                        beg=start_date.replace("-", ""),
                        end=end_date.replace("-", ""),
                        fqt=0  # 不复权
                    )
                
                if df_unadj is None or df_unadj.empty:
                    import akshare as ak
                    if code.startswith(("1", "5")):
                        df_unadj = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="")
                    elif is_index:
                        df_unadj = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
                    else:
                        df_unadj = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="")
            else:
                df_unadj = df
            
            return {
                "adjusted": df,
                "unadj": df_unadj if df_unadj is not None else df,
                "adjust_type": adjust,
            }
        except Exception as e:
            eastmoney_ok = False
            logger.warning(f"获取K线数据失败 {code}: {e}；尝试兜底数据源...")
            return self._fallback_kline_result(
                code, start_date, end_date, adjust, symbol, include_unadjusted, status
            )
        finally:
            _KLINE_BREAKER.record(success=eastmoney_ok)

    def _fallback_kline_result(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str,
        symbol: str = None,
        include_unadjusted: bool = True,
        status: dict = None,
    ) -> Optional[Dict]:
        """Build the same result shape from the fallback providers alone.

        这里对同一个标的取**两次**（复权 + 不复权），是这条路径最贵的一段。
        2026-09-05 部署机上兜底变成常态路径之后，_fetch_kline_sync 占了全部取数
        时间的 63.8%，所以查过能不能省掉第二次。结论是不能，记在这里免得重查：

        - 不复权序列喂 ``close_unadj`` -> ``CLOSE2``，被 research.py 的市盈率(静)
          用来取"当前价格"，去掉就少一维。
        - 看着可以省：前复权的定义就是把最新那根归一到真实价，所以 ``[-1]`` 两者
          相等。但这只在"区间结尾正好是最新交易日"时成立；钉了 ``date=`` 的查询
          里 qfq 仍按今天归一，最后一根不等于那天的实际收盘，省掉会把市盈率(静)
          算错。而钉日期恰恰是基线比对天天在走的路径。
        - 改成两次并发取能省一半墙钟，但会把对腾讯/新浪的并发翻倍，而它们在同一
          轮里本来就在 Max retries——收益和副作用都说不清量级，所以不动。
        """
        df = self._fetch_fallback_kline_sync(
            code, start_date, end_date, adjust, symbol, status
        )
        if df is None or df.empty:
            return None
        if adjust != "none" and include_unadjusted:
            df_unadj = self._fetch_fallback_kline_sync(
                code, start_date, end_date, "none", symbol
            )
        else:
            df_unadj = df

        # 行情只取一次，两个序列共用：复权与否不影响当天这根 bar 的原始价格。
        from . import intraday_quote

        quote = intraday_quote.resolve(symbol or code, require_ohlc=True)
        df = append_intraday_bar(df, quote, adjust=adjust, not_after=end_date)
        if df_unadj is not None and not df_unadj.empty:
            df_unadj = append_intraday_bar(
                df_unadj, quote, adjust="none", not_after=end_date
            )
        return {
            "adjusted": df,
            "unadj": df_unadj if df_unadj is not None and not df_unadj.empty else df,
            "adjust_type": adjust,
        }

    def _fetch_fallback_kline_sync(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str,
        symbol: str = None,
        status: dict = None,
    ):
        """问兜底那一层要一段历史行情。

        源本身（腾讯、新浪，以后的同花顺、雪球）都在 ``kline_source`` 里，这里只
        负责把参数包成一个请求。加一个源不用碰这个函数。
        """
        result = kline_source.resolve(
            kline_source.KlineRequest(
                code=code,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
                symbol=symbol,
            ),
            status={} if status is None else status,
        )
        return None if result is None else result.frame

    def fetch_kline_simple_sync(
        self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> Optional[Dict]:
        """
        简单获取 K 线数据（同步方法，返回简化的字典格式）
        """
        code, market = self._symbol_to_akshare(symbol)
        status: dict = {}
        kline_data = self._fetch_kline_sync(
            code,
            start_date,
            end_date,
            adjust,
            symbol,
            False,
            status,
        )
        
        if kline_data is None:
            # 区分"兜底源都不支持这个标的"和"该区间没有交易数据"。
            if status.get("unsupported"):
                return {"symbol": symbol, "adjust": adjust, "data": [], "unsupported": True}
            return None
        
        df = kline_data["adjusted"]
        adjust_type = kline_data["adjust_type"]
        
        # 转换为简单的字典列表格式
        result = []
        for _, row in df.iterrows():
            result.append({
                "日期": str(row["日期"]),
                "开盘": float(row["开盘"]),
                "收盘": float(row["收盘"]),
                "最高": float(row["最高"]),
                "最低": float(row["最低"]),
                "成交量": int(row["成交量"]),
                "成交额": float(row["成交额"]),
                "振幅": float(row["振幅"]) if "振幅" in row else 0,
                "涨跌幅": float(row["涨跌幅"]) if "涨跌幅" in row else 0,
                "涨跌额": float(row["涨跌额"]) if "涨跌额" in row else 0,
                "换手率": float(row["换手率"]) if "换手率" in row else 0,
            })
        
        return {
            "symbol": symbol,
            "adjust": {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}.get(adjust_type, adjust_type),
            "data": result,
        }
    
    async def fetch_kline_simple(
        self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> Optional[Dict]:
        """异步获取 K 线数据"""
        return await _run_in_executor(
            self.fetch_kline_simple_sync, symbol, start_date, end_date, adjust
        )
    
    def _fetch_finance_sync(self, code: str, symbol: str = None) -> Optional[Dict]:
        """同步获取财务数据"""
        from ..symbols import get_symbol_name
        symbol_name = get_symbol_name(symbol) if symbol else ""
        if code.startswith(("1", "5")) or check_is_index(symbol, symbol_name):
            return None
        try:
            import akshare as ak
            df = ak.stock_financial_abstract_ths(symbol=code)
            if df is None or df.empty:
                return _fetch_failure("finance")
            return {"finance": df}
        except Exception as e:
            logger.warning(f"获取财务数据失败 {code}: {e}")
            return _fetch_failure("finance")

    async def _fetch_finance_cached(self, code: str, symbol: str) -> Optional[Dict]:
        """Return a copied finance result without submitting cache hits to the executor."""
        cache_key = symbol.upper()
        now = time.monotonic()
        if FINANCE_CACHE_TTL_SECONDS > 0:
            with _finance_cache_lock:
                expired, _ = _prune_finance_cache(now)
                cached = _finance_cache.get(cache_key)
                if cached is not None and now - cached[0] <= FINANCE_CACHE_TTL_SECONDS:
                    cached_at, cached_result = cached
                    result = {"finance": cached_result["finance"].copy(deep=True)}
                else:
                    result = None
                cache_size = len(_finance_cache)
            if result is not None:
                request_id, tool, _ = log_context()
                logger.debug(
                    "Finance cache request_id=%s tool=%s symbol=%s "
                    "cache=hit age=%.1fs size=%s expired=%s",
                    request_id,
                    tool,
                    symbol,
                    now - cached_at,
                    cache_size,
                    expired,
                )
                return result

        request_id, tool, _ = log_context()
        inflight = _get_finance_inflight()
        task = inflight.get(cache_key)
        role = "follower"
        if task is None or task.done():
            task = asyncio.create_task(
                self._fetch_and_cache_finance(code, symbol, cache_key)
            )
            inflight[cache_key] = task
            task.add_done_callback(
                lambda completed, registry=inflight, key=cache_key: _complete_finance_inflight(
                    registry,
                    key,
                    completed,
                )
            )
            role = "leader"

        wait_started_at = time.perf_counter()
        result = await asyncio.shield(task)
        logger.debug(
            "Finance cache request_id=%s tool=%s symbol=%s cache=miss "
            "singleflight_role=%s wait=%.3fs",
            request_id,
            tool,
            symbol,
            role,
            time.perf_counter() - wait_started_at,
        )
        if result is None or "finance" not in result or result["finance"].empty:
            return result
        return {"finance": result["finance"].copy(deep=True)}

    async def _fetch_and_cache_finance(
        self,
        code: str,
        symbol: str,
        cache_key: str,
    ) -> Optional[Dict]:
        """Fetch and publish finance data even if the original caller disconnects."""
        result = await _run_in_executor(self._fetch_finance_sync, code, symbol)
        if result is None or "finance" not in result or result["finance"].empty:
            return result

        if FINANCE_CACHE_TTL_SECONDS > 0:
            with _finance_cache_lock:
                expired, evicted = _prune_finance_cache(
                    time.monotonic(),
                    reserve_entry=cache_key not in _finance_cache,
                )
                _finance_cache[cache_key] = (
                    time.monotonic(),
                    {"finance": result["finance"].copy(deep=True)},
                )
                cache_size = len(_finance_cache)
            if expired or evicted:
                logger.debug(
                    "Finance cache cleanup expired=%s evicted=%s size=%s",
                    expired,
                    evicted,
                    cache_size,
                )
        return {"finance": result["finance"].copy(deep=True)}
    
    def _fetch_fund_flow_sync(self, code: str, symbol: str = None) -> Optional[Dict]:
        """同步获取资金流向数据"""
        from ..symbols import get_symbol_name
        symbol_name = get_symbol_name(symbol) if symbol else ""
        is_index = check_is_index(symbol, symbol_name)
        
        try:
            import akshare as ak
            df = None
            is_market = False
            if is_index:
                exchange = "sh" if symbol.startswith("SH") else "sz"
                df = ak.stock_individual_fund_flow(stock=code, market=exchange)
            else:
                # 个股
                exchange = "sh" if code.startswith("6") else "sz"
                df = ak.stock_individual_fund_flow(stock=code, market=exchange)
            
            if df is None or df.empty:
                return _fetch_failure("fund_flow")
            return {"fund_flow": df, "is_market": is_market}
        except Exception as e:
            logger.warning(f"获取资金流向数据失败 {code}: {e}")
            return _fetch_failure("fund_flow")

    def _build_fund_flow_history(self, df, symbol: str, is_market: bool) -> Optional[Dict[str, np.ndarray]]:
        """Convert AkShare fund-flow rows to the internal report dataset."""
        if df is None or df.empty or "日期" not in df.columns:
            return None

        close_col = "收盘价"
        pct_col = "涨跌幅"
        if is_market:
            if symbol in {"SZ399001", "SZ399006"}:
                close_col = "深证-收盘价"
                pct_col = "深证-涨跌幅"
            else:
                close_col = "上证-收盘价"
                pct_col = "上证-涨跌幅"

        def to_float_array(column: str, scale: float = 1.0) -> np.ndarray:
            if column not in df.columns:
                return np.full(len(df), np.nan, dtype=np.float64)
            values = []
            for value in df[column].tolist():
                try:
                    values.append(float(value) * scale)
                except (TypeError, ValueError):
                    values.append(np.nan)
            return np.array(values, dtype=np.float64)

        try:
            dates = np.array([self._date_to_ns(d) for d in df["日期"]], dtype=np.int64)
        except Exception:
            return None

        return {
            "DATE": dates,
            "CLOSE": to_float_array(close_col),
            "PCT_CHG": to_float_array(pct_col, 0.01),
            "A_A": to_float_array("主力净流入-净额"),
            "A_R": to_float_array("主力净流入-净占比", 0.01),
            "XL_A": to_float_array("超大单净流入-净额"),
            "XL_R": to_float_array("超大单净流入-净占比", 0.01),
            "L_A": to_float_array("大单净流入-净额"),
            "L_R": to_float_array("大单净流入-净占比", 0.01),
            "M_A": to_float_array("中单净流入-净额"),
            "M_R": to_float_array("中单净流入-净占比", 0.01),
            "S_A": to_float_array("小单净流入-净额"),
            "S_R": to_float_array("小单净流入-净占比", 0.01),
        }
    
    async def _fetch_fund_flow_from_page(self, symbol: str) -> Optional[Dict]:
        """接口不可用时，从东财资金流向页面兜底取资金流向。

        页面走浏览器，不经过 requests，所以不消耗网关积分；代价是一次 Chromium
        页面加载。返回结构与 _fetch_fund_flow_sync 完全相同，下游的转换和渲染
        一行不用改——今日数值取的是历史表最后一行，与主源同一条路径。
        """
        if not FUND_FLOW_PAGE_ENABLED:
            return None

        from . import realtime_ff

        if realtime_ff.get_fund_flow_url(symbol) is None:
            # 这个标的根本没有资金流向页面（科创 50 这类指数就没有）。这件事在
            # 抢名额之前就能知道，必须在这里判——否则它会白等满一个等待窗口，
            # 而那段等待是从整个请求的预算里扣的，等于替同批的兄弟把预算花掉。
            #
            # 2026-09-05 部署机日志里就是这样：SH000688 等 3.0s 之后被判"名额已满"，
            # 同一批的 SH601899 / SH000001 / SZ399006 跟着一起没排到。
            logger.debug("资金流向页面兜底跳过 %s: 该标的没有资金流向页面", symbol)
            return None

        if _FUND_FLOW_PAGE_BREAKER.should_skip():
            # 页面和主源取的是同一个端点，主源被拒时页面的表也填不上。不熔断的
            # 话每次请求都要白付一次页面加载，把"缺一段"变成"慢三倍还是缺一段"。
            logger.debug("资金流向页面兜底跳过 %s: 熔断器打开", symbol)
            return None

        request_id, _, _ = log_context()
        wait_budget = _fund_flow_page_wait_budget(request_id)
        slots = _get_fund_flow_page_slots()
        try:
            await asyncio.wait_for(slots.acquire(), timeout=wait_budget)
        except (asyncio.TimeoutError, TimeoutError):
            # 名额是兜底层自己那 CONCURRENCY 个，不是 realtime_ff 的浏览器信号量。
            # 等不到就放弃而不是无限排队：排队会把"缺一段"换成"整批都慢"，而
            # 请求级预算已经表达了"这一批整体愿意为补全等多久"。
            reason = (
                f"请求预算 {FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS:.0f}s 已用尽"
                if wait_budget <= 0
                else f"等 {wait_budget:.1f}s 未排到"
            )
            logger.info(
                "资金流向页面兜底跳过 %s: 兜底名额已满(上限 %d)，%s",
                symbol,
                FUND_FLOW_PAGE_CONCURRENCY,
                reason,
            )
            return None

        started_at = time.perf_counter()

        try:
            page = await realtime_ff.fetch_history_page(symbol)
        except realtime_ff.FundFlowPageUnavailable:
            # 兜底的兜底：上面已经用 get_fund_flow_url 提前拦过一次，走到这里说明
            # 那两处的判断分叉了。仍然不计进熔断器——这跟数据源的健康状况无关，
            # 计进去的话查几次这种标的就会把兜底整层关掉一整个冷却期。
            logger.debug("资金流向页面兜底跳过 %s: 该标的没有资金流向页面", symbol)
            return None
        except realtime_ff.FundFlowPageRefused as e:
            # 与普通失败用同一个冷却：实测被拒是逐次随机的，8 轮里有 3 轮当场重试
            # 就能成功，长时间退避只会把本可以拿到的数据挡在外面。
            logger.warning("资金流向页面接口被拒 %s: %s", symbol, e)
            _FUND_FLOW_PAGE_BREAKER.record(success=False)
            return None
        except Exception as e:
            # 带上异常类型：FundFlowPageUnavailable 这类异常的 str() 就是标的本身，
            # 只打 %s 的话日志是"兜底失败 SH000688: SH000688"，读不出任何原因。
            logger.warning(
                "资金流向页面兜底失败 %s: %s: %s", symbol, type(e).__name__, e
            )
            _FUND_FLOW_PAGE_BREAKER.record(success=False)
            return None
        finally:
            slots.release()

        records = page.history_records()
        if not records:
            logger.warning("资金流向页面兜底无历史数据 %s", symbol)
            _FUND_FLOW_PAGE_BREAKER.record(success=False)
            return None
        _FUND_FLOW_PAGE_BREAKER.record(success=True)

        import pandas as pd

        logger.info(
            "资金流向页面兜底成功 %s rows=%d cost=%.3fs",
            symbol,
            len(records),
            time.perf_counter() - started_at,
        )
        # is_market 与主源保持一致：_fetch_fund_flow_sync 从不置 True，页面上的
        # 列名也是"收盘价/涨跌幅"这一套，不是指数那套带交易所前缀的列。
        return {"fund_flow": pd.DataFrame(records), "is_market": False}

    def _fetch_dividend_sync(self, code: str) -> Optional[Dict]:
        """同步获取分红数据"""
        # Note: dividend sync isn't passed symbol, but wait, does fetch_stock_data pass symbol?
        # Let me see. We need to optionally pass symbol to dividend_sync.
        if code.startswith(("1", "5")):
            return None
        try:
            import akshare as ak
            symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
            df = ak.stock_fhps_detail_em(symbol=symbol)
            if df is None or df.empty:
                return None
            return {"dividend": df}
        except Exception as e:
            logger.warning(f"获取分红数据失败 {code}: {e}")
            return None
    
    def _fetch_realtime_sync(self, code: str, symbol: str = None) -> Optional[Dict]:
        """取基本数据。逐级回退见 datasource/basic_info.py。

        这里只做三件事：决定用什么代码去查、按标的类别决定哪些字段该留、把结果映射
        成下游那七个键。取数本身交给 basic_info 那一层，加源去源都不用动这里。

        为什么按类别裁字段，而不是有什么就给什么：
          - ETF 的市值一直是 0（保留改动前的行为）。腾讯其实给得出 193.07 亿，
            但那会让 ETF 报告凭空多出几维，属于新功能不是补缺，得单独决定。
          - 指数同理。腾讯给上证 694637 亿、市盈率 17.06，而现在的报告里指数只有
            代码/名称/日期。
        改这两条之前先想清楚要不要改，别让"修回退"顺手改了输出。
        """
        from ..symbols import get_symbol_name

        symbol_name = get_symbol_name(symbol) if symbol else ""
        is_index = check_is_index(symbol, symbol_name)
        is_etf = code.startswith(("1", "5"))
        # efinance 认的是不带前缀的六位码：实测 get_quote_snapshot("SH600519") 返回
        # 一个全 NaN 的 series，而 "600519" 才拿得到贵州茅台。指数是唯一的例外，
        # 按代码查不到，得按中文名。这三支的取法与改动前逐字一致，别顺手"统一"成
        # 带前缀的写法——那会让东财整条路静默失效，而腾讯兜底会把症状盖住。
        query = symbol_name if (is_index and symbol_name) else (symbol if is_index else code)

        # ETF 和指数本来就不取市值，就别为了它去问第二个源。
        # 第二个参数必须带市场前缀：腾讯拿 000001 会猜成深市的平安银行。
        info = basic_info.resolve(
            query,
            symbol or code,
            require_valuation=not (is_etf or is_index),
        )
        if info is None or (info.name is None and info.last is None):
            return _fetch_failure("realtime")

        mapped = {
            "股票简称": info.name or "",
            "最新价": info.last or 0.0,
            "总股本": 0.0,
            "总市值": 0.0,
            "流通市值": 0.0,
            "动态市盈率": 0.0,
        }
        if not is_etf and not is_index:
            mapped["总股本"] = info.total_shares or 0.0
            mapped["总市值"] = info.total_market_cap or 0.0
            mapped["流通市值"] = info.float_market_cap or 0.0
            mapped["动态市盈率"] = info.pe_ttm or 0.0
            if info.pb:
                # 有就给，没有就不放这个键——渲染层会退回本地的
                # 现价/每股净资产，那条路差得更开（见 research.py 的注释），
                # 但总比没有好。
                mapped["市净率"] = info.pb

        if info.source != "eastmoney":
            logger.info(
                "基本数据来源 %s symbol=%s 市值=%s 市盈率=%s 市净率=%s",
                info.source,
                symbol or code,
                "有" if info.total_market_cap else "无",
                "有" if info.pe_ttm else "无",
                "有" if info.pb else "无",
            )
        return {"info": mapped}
    
    def _fetch_sector_sync(self, code: str) -> List[str]:
        """同步获取所属板块"""
        try:
            df = ef.stock.get_belong_board(code)
            if df is not None and not df.empty:
                return df["板块名称"].tolist()
            return []
        except Exception as e:
            logger.warning(f"获取板块数据失败 {code}: {e}")
            return []
    
    async def fetch_stock_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> StockData:
        """获取股票完整数据"""
        return await self.fetch_stock_data_with_requirements(symbol, start_date, end_date)

    async def fetch_stock_data_with_requirements(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        requirements: Optional[FetchRequirements] = None,
    ) -> StockData:
        """按工具需求获取股票数据。"""
        requirements = requirements or FetchRequirements()
        code, market = self._symbol_to_akshare(symbol)
        canonical_symbol = self._get_canonical_symbol(code, market)

        task_specs = [
            (
                "kline",
                _run_in_executor(
                    self._fetch_kline_sync,
                    code,
                    start_date,
                    end_date,
                    "qfq",
                    canonical_symbol,
                    requirements.unadjusted_kline,
                ),
            )
        ]
        if requirements.finance:
            task_specs.append(
                ("finance", self._fetch_finance_cached(code, canonical_symbol))
            )
        if requirements.fund_flow:
            task_specs.append(
                ("fund_flow", _run_in_executor(self._fetch_fund_flow_sync, code, canonical_symbol))
            )
        if requirements.realtime:
            task_specs.append(
                ("realtime", _run_in_executor(self._fetch_realtime_sync, code, canonical_symbol))
            )

        task_results = await asyncio.gather(*(future for _, future in task_specs))
        fetched = dict(zip((name for name, _ in task_specs), task_results))

        if requirements.fund_flow and _is_fetch_failure(fetched.get("fund_flow")):
            # 页面兜底挂在 gather 之后：只有主源真的失败才付这一次页面加载，正常
            # 情况下这条路一次都不会走。必须在下面统计 fetch_failures 之前替换，
            # 否则兜底成功了报告依然被判定为不完整而整体不进缓存。
            page_result = await self._fetch_fund_flow_from_page(canonical_symbol)
            if page_result is not None:
                fetched["fund_flow"] = page_result

        kline_data = fetched.get("kline")
        finance_data = fetched.get("finance")
        fund_flow_data = fetched.get("fund_flow")
        realtime_data = fetched.get("realtime")
        
        stock_data = StockData(symbol=canonical_symbol)
        stock_data.fetch_failures = [
            str(result[_FETCH_FAILURE_MARKER])
            for result in fetched.values()
            if _is_fetch_failure(result)
        ]
        
        if realtime_data and "info" in realtime_data:
            info = realtime_data["info"]
            stock_data.name = info.get("股票简称", "")
            
            total_shares = info.get("总股本", 0)
            stock_data.total_shares = np.array([self._safe_float(total_shares)])
            
            total_market_cap = info.get("总市值", 0)
            stock_data.total_market_cap = np.array([self._safe_float(total_market_cap)])
            
            float_market_cap = info.get("流通市值", 0)
            stock_data.float_market_cap = np.array([self._safe_float(float_market_cap)])
            
            latest_price = self._safe_float(info.get("最新价", 0))
            if latest_price > 0:
                float_shares = self._safe_float(float_market_cap) / latest_price
                stock_data.float_shares = np.array([float(float_shares)])
            else:
                stock_data.float_shares = np.array([0.0])
            
            pe_ttm = info.get("动态市盈率", 0)
            stock_data.pe_ttm = np.array([self._safe_float(pe_ttm)])

            pb = info.get("市净率", 0)
            stock_data.pb = np.array([self._safe_float(pb)])
        
        if kline_data:
            df_qfq = kline_data.get("adjusted")
            df_unadj = kline_data.get("unadj")
            
            if df_qfq is not None and not df_qfq.empty:
                stock_data.date = np.array([self._date_to_ns(d) for d in df_qfq["日期"]], dtype=np.int64)
                stock_data.open = df_qfq["开盘"].values.astype(np.float64)
                stock_data.high = df_qfq["最高"].values.astype(np.float64)
                stock_data.low = df_qfq["最低"].values.astype(np.float64)
                stock_data.close = df_qfq["收盘"].values.astype(np.float64)
                stock_data.volume = df_qfq["成交量"].values.astype(np.float64)
                stock_data.amount = df_qfq["成交额"].values.astype(np.float64)
                
                if df_unadj is not None and not df_unadj.empty:
                    stock_data.close_unadj = df_unadj["收盘"].values.astype(np.float64)
                else:
                    stock_data.close_unadj = stock_data.close.copy()
                
                n = len(stock_data.date)
                stock_data.given_cash = np.zeros(n, dtype=np.float64)
                stock_data.given_share = np.zeros(n, dtype=np.float64)
        
        if finance_data and "finance" in finance_data:
            df = finance_data["finance"]
            if not df.empty:
                try:
                    if "报告期" in df.columns:
                        stock_data.finance_date = np.array(
                            [self._date_to_ns(d) for d in df["报告期"]], 
                            dtype=np.int64
                        )
                    if "基本每股收益" in df.columns:
                        stock_data.eps = self._parse_numeric_column(df["基本每股收益"])
                    if "每股净资产" in df.columns:
                        stock_data.nav_per_share = self._parse_numeric_column(df["每股净资产"])
                    if "净资产收益率" in df.columns:
                        stock_data.roe = self._parse_numeric_column(df["净资产收益率"], is_percent=True)
                    if "营业总收入" in df.columns:
                        stock_data.main_revenue = self._parse_numeric_column(df["营业总收入"])
                    if "净利润" in df.columns:
                        stock_data.net_profit = self._parse_numeric_column(df["净利润"])
                except Exception as e:
                    logger.warning(f"处理财务数据失败: {e}")
                    stock_data.fetch_failures.append("finance")
        
        if fund_flow_data and "fund_flow" in fund_flow_data:
            df = fund_flow_data["fund_flow"]
            stock_data.is_market = fund_flow_data.get("is_market", False)
            if not df.empty:
                try:
                    stock_data.fund_flow_history = self._build_fund_flow_history(
                        df, canonical_symbol, stock_data.is_market
                    )
                    if stock_data.fund_flow_history is None:
                        stock_data.fetch_failures.append("fund_flow")
                    latest = df.iloc[-1] if len(df) > 0 else None
                    if latest is not None:
                        # 字段映射表：(DataFrame字段名, StockData属性名, 是否为占比)
                        fields = [
                            ("主力净流入-净额", "fund_main_amount", False),
                            ("主力净流入-净占比", "fund_main_ratio", True),
                            ("超大单净流入-净额", "fund_xl_amount", False),
                            ("超大单净流入-净占比", "fund_xl_ratio", True),
                            ("大单净流入-净额", "fund_l_amount", False),
                            ("大单净流入-净占比", "fund_l_ratio", True),
                            ("中单净流入-净额", "fund_m_amount", False),
                            ("中单净流入-净占比", "fund_m_ratio", True),
                            ("小单净流入-净额", "fund_s_amount", False),
                            ("小单净流入-净占比", "fund_s_ratio", True),
                        ]
                        for df_col, attr, is_ratio in fields:
                            if df_col in df.columns:
                                val = latest.get(df_col, 0)
                                if is_ratio:
                                    val = val / 100.0  # 转换为 0.0-1.0
                                setattr(stock_data, attr, np.array([val], dtype=np.float64))
                except Exception as e:
                    logger.warning(f"处理资金流向数据失败: {e}")
                    stock_data.fetch_failures.append("fund_flow")

        stock_data.fetch_failures = list(dict.fromkeys(stock_data.fetch_failures))
        
        return stock_data
    
    async def fetch_stock_list(self) -> List[Dict[str, str]]:
        """获取股票列表"""
        def _fetch():
            df = ef.stock.get_realtime_quotes()
            result = []
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row["股票代码"])
                    name = str(row["股票名称"])
                    prefix = "SH" if code.startswith(("6", "5")) else "SZ"
                    result.append({"code": f"{prefix}{code}", "name": name})
            return result
        
        return await _run_in_executor(_fetch)
