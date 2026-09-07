"""上游源熔断器。

从 cn_stock_source 抽出来独立成模块，是因为浏览器层（realtime_ff）也要用同一个：
实时资金流的页面加载原先没有熔断，滑块一出现照样一个标的接一个标的地连发。
cn_stock_source 仍从这里 import 并原名导出，旧引用不用改。
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Optional

from ..config import SOURCE_BREAKER_ENABLED
from ..observability import log_context
from .http_channel import installed_mode

logger = logging.getLogger("finmcp")


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
