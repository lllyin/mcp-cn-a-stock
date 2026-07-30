import asyncio
import fcntl
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from playwright.async_api import Browser, Playwright, async_playwright


logger = logging.getLogger("qtf_mcp")


MARKET_BREADTH_RANGES = (
    "跌停 ~ -8%",
    "-8% ~ -6%",
    "-6% ~ -4%",
    "-4% ~ -2%",
    "-2% ~ 0%",
    "0% ~ 2%",
    "2% ~ 4%",
    "4% ~ 6%",
    "6% ~ 8%",
    "8% ~ 涨停",
)


@dataclass(frozen=True)
class MarketBreadthBucket:
    label: str
    count: int


@dataclass(frozen=True)
class MarketBreadthData:
    source: str
    fetched_at: str
    up_count: int
    down_count: int
    flat_count: int
    limit_up_count: Optional[int]
    limit_down_count: Optional[int]
    distribution: tuple[MarketBreadthBucket, ...]
    trade_date: Optional[str] = None
    market_time: Optional[str] = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TonghuashunAuth:
    v_cookie: str
    user_agent: str


class MarketBreadthProvider(Protocol):
    name: str

    async def fetch(self) -> MarketBreadthData:
        ...


class MarketBreadthUnavailable(RuntimeError):
    pass


class TonghuashunAuthError(RuntimeError):
    pass


class TonghuashunCooldownError(RuntimeError):
    pass


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _now_shanghai() -> str:
    return _shanghai_now().strftime("%Y-%m-%d %H:%M:%S")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _default_auth_cache_path() -> Path:
    configured = os.getenv("CN_STOCK_TONGHUASHUN_AUTH_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / ".runtime" / "tonghuashun-auth.json"


def _build_buckets(counts: Iterable[int]) -> tuple[MarketBreadthBucket, ...]:
    values = [int(value) for value in counts]
    if len(values) != len(MARKET_BREADTH_RANGES):
        raise ValueError(f"涨跌分布应包含 10 档，实际为 {len(values)} 档")
    if any(value < 0 for value in values):
        raise ValueError("涨跌分布不能包含负数")
    return tuple(
        MarketBreadthBucket(label=label, count=count)
        for label, count in zip(MARKET_BREADTH_RANGES, values)
    )


def build_market_breadth_distribution(
    percentages: pd.Series,
) -> tuple[MarketBreadthBucket, ...]:
    """Build the same ten percentage-change buckets used by Tonghuashun."""
    values = pd.to_numeric(percentages, errors="coerce").dropna()
    counts = (
        int((values < -8).sum()),
        int(((values >= -8) & (values < -6)).sum()),
        int(((values >= -6) & (values < -4)).sum()),
        int(((values >= -4) & (values < -2)).sum()),
        int(((values >= -2) & (values < 0)).sum()),
        int(((values >= 0) & (values <= 2)).sum()),
        int(((values > 2) & (values <= 4)).sum()),
        int(((values > 4) & (values <= 6)).sum()),
        int(((values > 6) & (values <= 8)).sum()),
        int((values > 8).sum()),
    )
    return _build_buckets(counts)


def _normalize_trade_date(value: object) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return None


def _extract_tonghuashun_trade_date(payload: dict) -> Optional[str]:
    containers = (payload, payload.get("zdfb_data"), payload.get("zdt_data"))
    keys = ("trade_date", "tradeDate", "data_date", "dataDate", "date")
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            if key in container:
                trade_date = _normalize_trade_date(container[key])
                if trade_date is not None:
                    return trade_date
    return None


def parse_tonghuashun_market_breadth(payload: dict) -> MarketBreadthData:
    distribution_data = payload.get("zdfb_data") or {}
    limit_data = (payload.get("zdt_data") or {}).get("last_zdt") or {}
    distribution = _build_buckets(distribution_data.get("zdfb") or [])
    up_count = int(distribution_data["znum"])
    down_count = int(distribution_data["dnum"])
    flat_count = sum(bucket.count for bucket in distribution) - up_count - down_count
    if flat_count < 0:
        raise ValueError("涨跌分布总数小于上涨与下跌家数之和")

    times = (payload.get("zdt_data") or {}).get("zd_time") or []
    return MarketBreadthData(
        source="tonghuashun_web",
        fetched_at=_now_shanghai(),
        trade_date=_extract_tonghuashun_trade_date(payload),
        up_count=up_count,
        down_count=down_count,
        flat_count=flat_count,
        limit_up_count=int(limit_data["ztzs"]),
        limit_down_count=int(limit_data["dtzs"]),
        distribution=distribution,
        market_time=str(times[-1]) if times else None,
    )


def _build_tonghuashun_auth(cookies: list[dict], user_agent: str) -> TonghuashunAuth:
    v_cookie = next(
        (str(cookie.get("value", "")) for cookie in cookies if cookie.get("name") == "v"),
        "",
    )
    if not v_cookie:
        raise TonghuashunAuthError("同花顺页面未生成 v Cookie")
    if not user_agent:
        raise TonghuashunAuthError("无法获取浏览器 User-Agent")
    return TonghuashunAuth(v_cookie=v_cookie, user_agent=user_agent)


def _tonghuashun_browser_args() -> list[str]:
    args = [
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-blink-features=AutomationControlled",
        "--window-position=-10000,-10000",
    ]
    if _env_flag("CN_STOCK_CHROME_NO_SANDBOX"):
        args.append("--no-sandbox")
    return args


class TonghuashunPlaywrightProvider:
    name = "tonghuashun_web"
    page_url = "https://q.10jqka.com.cn/"
    api_url = "https://q.10jqka.com.cn/api.php?t=indexflash&"

    def __init__(
        self,
        cooldown_seconds: Optional[float] = None,
        auth_cache_path: Optional[str | os.PathLike[str]] = None,
    ) -> None:
        self._auth: TonghuashunAuth | None = None
        self._rejected_auth: TonghuashunAuth | None = None
        self._auth_lock = asyncio.Lock()
        self._session = requests.Session()
        self._session_lock = threading.Lock()
        self._cooldown_seconds = (
            _env_float("CN_STOCK_TONGHUASHUN_COOLDOWN_SECONDS", 300.0)
            if cooldown_seconds is None
            else max(0.0, cooldown_seconds)
        )
        self._cooldown_until = 0.0
        self._last_failure: Optional[str] = None
        self._auth_cache_path = (
            Path(auth_cache_path).expanduser()
            if auth_cache_path is not None
            else _default_auth_cache_path()
        )
        self._auth_lock_path = self._auth_cache_path.with_suffix(
            f"{self._auth_cache_path.suffix}.lock"
        )

    def _prepare_auth_cache_dir_sync(self) -> None:
        parent = self._auth_cache_path.parent
        if not parent.exists():
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _load_cached_auth_sync(self) -> TonghuashunAuth | None:
        try:
            with self._auth_cache_path.open("r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
            if not isinstance(payload, dict):
                return None
            if payload.get("version") != 1:
                return None
            return _build_tonghuashun_auth(
                [{"name": "v", "value": payload.get("v_cookie", "")}],
                str(payload.get("user_agent", "")),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, TonghuashunAuthError):
            return None

    def _save_cached_auth_sync(self, auth: TonghuashunAuth) -> None:
        self._prepare_auth_cache_dir_sync()
        fd, temporary_path = tempfile.mkstemp(
            prefix=".tonghuashun-auth-",
            dir=self._auth_cache_path.parent,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as cache_file:
                json.dump(
                    {
                        "version": 1,
                        "v_cookie": auth.v_cookie,
                        "user_agent": auth.user_agent,
                        "saved_at": _now_shanghai(),
                    },
                    cache_file,
                    ensure_ascii=False,
                )
                cache_file.flush()
                os.fsync(cache_file.fileno())
            os.replace(temporary_path, self._auth_cache_path)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    def _acquire_auth_file_lock_sync(self):
        self._prepare_auth_cache_dir_sync()
        fd = os.open(self._auth_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        lock_file = os.fdopen(fd, "r+")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return lock_file

    @staticmethod
    def _release_auth_file_lock_sync(lock_file) -> None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def _delete_cached_auth_if_stale_sync(self, stale_auth: TonghuashunAuth) -> None:
        lock_file = self._acquire_auth_file_lock_sync()
        try:
            if self._load_cached_auth_sync() == stale_auth:
                try:
                    self._auth_cache_path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            self._release_auth_file_lock_sync(lock_file)

    @staticmethod
    async def _launch_browser(playwright: Playwright) -> Browser:
        launch_args = {
            "headless": False,
            "args": _tonghuashun_browser_args(),
        }
        try:
            return await playwright.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            return await playwright.chromium.launch(**launch_args)

    async def _bootstrap_auth(self) -> TonghuashunAuth:
        async with async_playwright() as playwright:
            browser = await self._launch_browser(playwright)
            try:
                context = await browser.new_context(java_script_enabled=True, bypass_csp=True)
                page = await context.new_page()
                navigation = await page.goto(
                    self.page_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                if navigation is None or navigation.status != 200:
                    status = navigation.status if navigation else "unknown"
                    raise RuntimeError(f"同花顺页面返回 HTTP {status}")

                cookies: list[dict] = []
                for _ in range(24):
                    cookies = await context.cookies(self.page_url)
                    if any(cookie.get("name") == "v" for cookie in cookies):
                        break
                    await page.wait_for_timeout(250)
                user_agent = await page.evaluate("navigator.userAgent")
                return _build_tonghuashun_auth(cookies, str(user_agent))
            finally:
                await browser.close()

    async def _ensure_auth(self) -> TonghuashunAuth:
        if self._auth is not None:
            return self._auth
        async with self._auth_lock:
            if self._auth is None:
                cached_auth = await asyncio.to_thread(self._load_cached_auth_sync)
                if cached_auth is not None and cached_auth != self._rejected_auth:
                    self._auth = cached_auth
                    self._rejected_auth = None
                    return self._auth

                lock_file = None
                try:
                    lock_file = await asyncio.to_thread(self._acquire_auth_file_lock_sync)
                    cached_auth = await asyncio.to_thread(self._load_cached_auth_sync)
                    if cached_auth is not None and cached_auth != self._rejected_auth:
                        self._auth = cached_auth
                    else:
                        self._auth = await self._bootstrap_auth()
                        try:
                            await asyncio.to_thread(self._save_cached_auth_sync, self._auth)
                        except OSError as exc:
                            logger.warning("Unable to persist Tonghuashun auth cache: %s", exc)
                    self._rejected_auth = None
                except OSError as exc:
                    logger.warning("Unable to lock Tonghuashun auth cache: %s", exc)
                    self._auth = await self._bootstrap_auth()
                    self._rejected_auth = None
                finally:
                    if lock_file is not None:
                        await asyncio.to_thread(self._release_auth_file_lock_sync, lock_file)
            return self._auth

    async def _invalidate_auth(self, stale_auth: TonghuashunAuth) -> None:
        invalidated = False
        async with self._auth_lock:
            if self._auth == stale_auth:
                self._auth = None
                self._rejected_auth = stale_auth
                invalidated = True
        if invalidated:
            try:
                await asyncio.to_thread(self._delete_cached_auth_if_stale_sync, stale_auth)
            except OSError as exc:
                logger.warning("Unable to remove stale Tonghuashun auth cache: %s", exc)

    def _request_payload_sync(self, auth: TonghuashunAuth) -> dict:
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Cookie": f"v={auth.v_cookie}",
            "Referer": self.page_url,
            "User-Agent": auth.user_agent,
            "X-Requested-With": "XMLHttpRequest",
        }
        with self._session_lock:
            response = self._session.get(self.api_url, headers=headers, timeout=15)
        if response.status_code == 403:
            raise TonghuashunAuthError("同花顺涨跌分布接口返回 HTTP 403")
        if response.status_code != 200:
            raise RuntimeError(f"同花顺涨跌分布接口返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except (json.JSONDecodeError, requests.JSONDecodeError, ValueError) as exc:
            raise TonghuashunAuthError("同花顺涨跌分布接口未返回 JSON") from exc
        if not isinstance(payload, dict):
            raise TonghuashunAuthError("同花顺涨跌分布接口返回格式异常")
        return payload

    async def fetch(self) -> MarketBreadthData:
        now = time.monotonic()
        if now < self._cooldown_until:
            remaining = max(1, int(self._cooldown_until - now))
            detail = f": {self._last_failure}" if self._last_failure else ""
            raise TonghuashunCooldownError(f"同花顺数据源冷却中，约 {remaining} 秒后重试{detail}")

        try:
            auth = await self._ensure_auth()
            for attempt in range(2):
                try:
                    payload = await asyncio.to_thread(self._request_payload_sync, auth)
                    result = parse_tonghuashun_market_breadth(payload)
                    self._cooldown_until = 0.0
                    self._last_failure = None
                    return result
                except TonghuashunAuthError:
                    await self._invalidate_auth(auth)
                    if attempt == 1:
                        raise
                    auth = await self._ensure_auth()
            raise AssertionError("unreachable")
        except Exception as exc:
            self._last_failure = str(exc)
            self._cooldown_until = time.monotonic() + self._cooldown_seconds
            raise

    def close(self) -> None:
        with self._session_lock:
            self._session.close()


class EfinanceMarketBreadthProvider:
    name = "efinance"

    @staticmethod
    def _fetch_sync() -> MarketBreadthData:
        import efinance as ef

        quotes = ef.stock.get_realtime_quotes()
        if quotes is None or quotes.empty or "涨跌幅" not in quotes.columns:
            raise RuntimeError("efinance 未返回有效的全市场行情")

        percentages = pd.to_numeric(quotes["涨跌幅"], errors="coerce")
        valid = percentages.dropna()
        distribution = build_market_breadth_distribution(valid)
        trade_date = None
        if "最新交易日" in quotes.columns:
            dates = quotes.loc[percentages.notna(), "最新交易日"].dropna().astype(str)
            if not dates.empty:
                trade_date = dates.mode().iloc[0]

        return MarketBreadthData(
            source="efinance",
            fetched_at=_now_shanghai(),
            trade_date=trade_date,
            up_count=int((valid > 0).sum()),
            down_count=int((valid < 0).sum()),
            flat_count=int((valid == 0).sum()),
            limit_up_count=None,
            limit_down_count=None,
            distribution=distribution,
            warnings=("efinance 备用源不提供可靠的涨停、跌停家数",),
        )

    async def fetch(self) -> MarketBreadthData:
        return await asyncio.to_thread(self._fetch_sync)


DEFAULT_MARKET_BREADTH_PROVIDERS: tuple[MarketBreadthProvider, ...] = (
    TonghuashunPlaywrightProvider(),
    EfinanceMarketBreadthProvider(),
)

_market_breadth_cache: tuple[float, MarketBreadthData] | None = None
_market_breadth_fetch_lock = asyncio.Lock()


def _market_breadth_cache_ttl(now: datetime | None = None) -> float:
    current = now or _shanghai_now()
    hhmm = current.hour * 100 + current.minute
    if current.weekday() < 5 and 915 <= hhmm <= 1510:
        return 15.0
    return 300.0


def _get_cached_market_breadth() -> MarketBreadthData | None:
    if _market_breadth_cache is None:
        return None
    expires_at, data = _market_breadth_cache
    return data if time.monotonic() < expires_at else None


async def _fetch_from_providers(
    providers: Sequence[MarketBreadthProvider],
) -> MarketBreadthData:
    failures = []
    for provider in providers:
        try:
            result = await provider.fetch()
            if failures:
                result = replace(result, warnings=result.warnings + tuple(failures))
            return result
        except Exception as exc:
            failures.append(f"{provider.name} 不可用: {exc}")
    raise MarketBreadthUnavailable("; ".join(failures) or "没有可用的涨跌分布数据源")


async def get_market_breadth(
    providers: Optional[Sequence[MarketBreadthProvider]] = None,
) -> MarketBreadthData:
    """Fetch market breadth from the first available provider."""
    global _market_breadth_cache
    if providers is not None:
        return await _fetch_from_providers(providers)

    cached = _get_cached_market_breadth()
    if cached is not None:
        return cached

    async with _market_breadth_fetch_lock:
        cached = _get_cached_market_breadth()
        if cached is not None:
            return cached
        result = await _fetch_from_providers(DEFAULT_MARKET_BREADTH_PROVIDERS)
        _market_breadth_cache = (time.monotonic() + _market_breadth_cache_ttl(), result)
        return result


def close_market_breadth_resources() -> None:
    for provider in DEFAULT_MARKET_BREADTH_PROVIDERS:
        close = getattr(provider, "close", None)
        if close is not None:
            close()
