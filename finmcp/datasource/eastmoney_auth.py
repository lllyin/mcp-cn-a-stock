"""东财 ``nid18`` 凭据：采一次、存盘、给之后的纯 API 请求带上。

## 为什么需要它

行情列表、基本快照、普通 K 线和资金流日线均出现过普通请求被断开，而浏览器里可访问。
同一时刻交错测试中，带页面签发的 ``nid18`` 比不带更稳定；伪造同名 Cookie 无此效果。
端点本身仍会间歇拒绝，所以凭据只提高成功率，后面的备用源仍然保留。

## 为什么只能用浏览器采

没有任何一个东财主机用 ``Set-Cookie`` 下发它（push2 / www / quote / data 四个主机
的响应头里 Set-Cookie 条数都是 0）——这个值是页面里的 JS 写的，同时写一个
``nid18_create_time``。所以只有能跑 JS 的浏览器拿得到，和 ``market_breadth`` 取同花顺
``v`` Cookie 是同一个形态。

值的稳定性量过两轮，结论是**同期稳定、跨期轮换**：连着采三次（三个互不相干的浏览器
进程）拿到同一个值，但二十多分钟后再采就是另一个值了。Cookie 自带的过期时间写着
90 天，**复用周期不能按它取**——既然值会轮换，而且伪造值不放行（说明服务端认得出
哪些是它见过的），那服务端一侧就还有一份有效期不可观测的登记。

所以 TTL 只决定何时刷新，不是硬失效时间：新值到手前继续使用旧值；连续被拒也会
触发重采。凭据只影响请求能否通过，不改变返回数据，保留旧值不会造成数据陈旧。

## 两条采集路径

1. **顺手采**。资金流页面兜底本来就在共享浏览器里打开 ``data.eastmoney.com``，那次
   加载就会写 ``.eastmoney.com`` 的 ``nid18``。``remember_cookies`` 从那个 context
   里读一次，一分钱不花。
2. **独立采**。没有任何页面加载发生过时，后台起一个短命浏览器专门采一次。

刻意**不复用** ``realtime_ff`` 的共享浏览器：那个 context 绑在创建它的事件循环上，
而这里的后台采集跑在自己的循环里。在后台循环里把它建起来，之后请求路径再从别的
循环去用同一个对象，是个跨循环复用的坑。宁可多付一个短命进程。

## 这一层不许做的事

取凭据失败一律降级成"不带 Cookie"，绝不抛给请求路径——那样就把一个可用性优化变成了
新的单点故障。所有对外入口都不抛异常（AGENTS §一）。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from ..config import (
    EASTMONEY_AUTH_ENABLED,
    EASTMONEY_AUTH_HARVEST_TIMEOUT_SECONDS,
    EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES,
    EASTMONEY_AUTH_PAGE,
    EASTMONEY_AUTH_TTL_SECONDS,
)

logger = logging.getLogger("finmcp")

#: Cookie 名。值由页面 JS 写入，服务端认值。
COOKIE_NAME = "nid18"

#: 要求这个 Cookie 的主机。**只列量过的**：``fund.eastmoney.com`` 和
#: ``emweb.securities.eastmoney.com`` 也在伪装通道的接管名单里，但实测带与不带
#: 都是 6/6，不需要这一层，列进来只会让影响面比证据大。
AUTH_HOSTS = ("push2.eastmoney.com", "push2his.eastmoney.com")

#: 只覆盖量过且确认凭据有帮助的接口；同主机的其他路径不自动带入。
AUTH_PATHS = {
    "push2.eastmoney.com": (
        "/api/qt/clist/get",
        "/api/qt/stock/get",
    ),
    "push2his.eastmoney.com": (
        "/api/qt/stock/fflow/daykline/get",
        "/api/qt/stock/kline/get",
    ),
}

_CACHE_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "eastmoney-auth.json"
_PAYLOAD_VERSION = 1

_lock = threading.Lock()
#: 手上这个值，以及它是什么时候采到的。None 表示还没有。
_value: Optional[str] = None
_harvested_at: float = 0.0
#: 采集时浏览器自报的 UA。存着是为了排查"值和 UA 绑不绑"，**不**拿它改请求头——
#: 那会顺带改掉这几个主机的请求身份，超出这一层该管的范围。
_user_agent: str = ""
#: 带着凭据仍然连续失败的次数。到阈值就怀疑它失效。
_failures: int = 0
#: 手上这个值被怀疑失效，正在等一次重采来判。**怀疑期间照旧使用它**——见 note_outcome。
_suspect: bool = False
#: 后台采集是否在飞。同一时刻最多一个：采集要开浏览器，而请求路径是并发的。
_harvesting = False
#: 上一次采集失败的时间。用来给失败后的重试留一个间隔，不然每个请求都会踢一次。
_last_attempt_at: float = 0.0


def _now() -> float:
    return time.monotonic()


def enabled() -> bool:
    """这一层开着没有。

    做成函数而不是让调用方直接读常量：开关只在这一个模块里有一份，测试也只需要
    改这一个地方。
    """
    return bool(EASTMONEY_AUTH_ENABLED)


def has_credential() -> bool:
    """当前是否有可继续尝试的凭据。TTL 到期的旧值在刷新完成前仍算可用。"""
    with _lock:
        return bool(_value)


def needs_auth(url) -> bool:
    """这个 URL 要不要带凭据。

    按解析出的 hostname 判，不按整个 URL 做子串匹配：查询参数里恰好出现某个主机名
    的请求（东财的页面 URL 常被当参数传）不该因此被当成 API 请求。
    """
    if not enabled():
        return False
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if host not in AUTH_HOSTS:
        return False
    path = parts.path.lower()
    return any(path.endswith(prefix) for prefix in AUTH_PATHS[host])


def _expired(harvested_at: float) -> bool:
    return _now() - harvested_at >= EASTMONEY_AUTH_TTL_SECONDS


def cookie_header(url) -> str:
    """给这个 URL 的 ``Cookie`` 头，没有凭据就返回空串。

    **不阻塞、不采集。** 这个函数在请求路径上，而采集要开浏览器；在这里等一个页面
    加载会把每个首次请求都拖上几秒。没有值就返回空串走老路，同时踢一次后台采集，
    让**后面**的请求受益。
    """
    if not needs_auth(url):
        return ""
    with _lock:
        value, harvested_at = _value, _harvested_at
    if not value:
        ensure_ready()
        return ""
    if _expired(harvested_at):
        # TTL 是刷新间隔，不是硬失效时间。新值到手前继续带旧值：上游不可用时
        # 清掉一个可能仍有效的凭据，只会让请求退回成功率更低的无凭据路径。
        ensure_ready()
    return f"{COOKIE_NAME}={value}"


def note_outcome(url, *, success: bool) -> None:
    """记一次带凭据请求的结果，连续失败到阈值就去重采一次。

    单次失败不判：这几个端点本身就逐次随机地拒，带着有效凭据也会偶尔空手，按单次
    判会让每次抖动都换掉一个好值，而换一次要付一个页面加载。

    到阈值也**不丢掉手上这个值**，只是标记可疑并去重采。这一条是实测改的：上游进
    一个"谁都取不到"的窗口时（带真凭据和不带都是 0/6），按"连拒就丢"会把一个完好
    的凭据删掉，于是窗口期内连 Cookie 都不带了——比改动之前更差。

    重采一次总是安全的：值同期稳定，重采多半拿回同一个，而那次页面加载会把它在
    服务端重新登记一遍，正好覆盖"值没错、登记过期了"。值真的轮换过了就换上新的。
    """
    global _failures, _suspect
    if not needs_auth(url):
        return
    with _lock:
        if _value is None:
            return
        if success:
            _failures = 0
            _suspect = False
            return
        _failures += 1
        if _failures < EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES:
            return
        _failures = 0
        if _suspect:
            # 已经在等重采了，不必再喊一遍。
            return
        _suspect = True
    logger.warning(
        "eastmoney_auth 凭据连续 %s 次被拒，去重采一次（期间继续使用手上这个）host=%s",
        EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES,
        (urlsplit(url).hostname or "?"),
    )
    ensure_ready()


def _store(value: str, user_agent: str = "") -> bool:
    """记下一个新值。返回它是不是真的换了。"""
    global _value, _harvested_at, _user_agent, _failures, _suspect
    if not value:
        return False
    with _lock:
        changed = value != _value
        _value = value
        _harvested_at = _now()
        _failures = 0
        # 采回来了就不再可疑：值换了固然算解决，值没换也算——那次页面加载已经把它
        # 在服务端重新登记过，能修的都修了，再挂着可疑只会让它每轮都重采一次。
        _suspect = False
        if user_agent:
            _user_agent = user_agent
    return changed


def remember_cookies(cookies, user_agent: str = "") -> bool:
    """从任何一个浏览器 context 的 cookie 列表里挑出凭据。

    这是"顺手采"那条路：调用方本来就为别的事加载了东财页面，这里只是读一次。
    **不抛异常**——调用点在取数路径上，采凭据失败不该影响那次取数。
    """
    if not enabled():
        return False
    try:
        for cookie in cookies or ():
            if cookie.get("name") != COOKIE_NAME:
                continue
            value = str(cookie.get("value") or "")
            if not value:
                continue
            changed = _store(value, user_agent)
            # 同一个浏览器值会在每次资金流页面加载时被看到。只在变化时落盘，
            # 避免把“顺手读一次”变成每页一次 fsync。
            if changed:
                _write_cached(value, user_agent)
            if changed:
                logger.info("eastmoney_auth 顺手采到凭据 来源=既有浏览器页面")
            return True
    except Exception:
        logger.debug("eastmoney_auth 读取 cookie 失败", exc_info=True)
    return False


async def remember_from_context(context) -> bool:
    """``remember_cookies`` 的异步壳，直接吃 Playwright 的 BrowserContext。

    ``context`` 给 None 或给一个不像 context 的东西都只是返回 False。调用点在取数
    路径上，这一层**任何**问题都不许变成那次取数的问题——它自己就是靠"顺手"才划算的，
    值不到让一次取数失败。
    """
    if not enabled() or context is None:
        return False
    try:
        cookies = await context.cookies("https://push2.eastmoney.com/")
    except Exception:
        logger.debug("eastmoney_auth 从 context 取 cookie 失败", exc_info=True)
        return False
    return remember_cookies(cookies)


def invalidate(value=None) -> None:
    """丢弃凭据。给了值就只在仍是这个值时丢，避免踩掉别的线程刚采到的新值。"""
    global _value, _harvested_at, _failures, _suspect
    with _lock:
        if value is not None and _value != value:
            return
        _value = None
        _harvested_at = 0.0
        _failures = 0
        _suspect = False
    _delete_cached()


# --- 磁盘缓存 -----------------------------------------------------------------
# 存盘的理由是重启不必重采。文件权限 0600、写入走临时文件 + 原子替换，和
# market_breadth 存同花顺凭据同一形态。


def _prepare_dir() -> None:
    parent = _CACHE_PATH.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def _lock_file():
    _prepare_dir()
    fd = os.open(str(_CACHE_PATH) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(fd, "r+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _unlock(handle) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_cached() -> Optional[dict]:
    try:
        with _CACHE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _PAYLOAD_VERSION:
        return None
    if not payload.get(COOKIE_NAME):
        return None
    return payload


def _write_cached(value: str, user_agent: str) -> None:
    handle = None
    try:
        handle = _lock_file()
        _prepare_dir()
        fd, temporary = tempfile.mkstemp(prefix=".eastmoney-auth-", dir=_CACHE_PATH.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"version": _PAYLOAD_VERSION, COOKIE_NAME: value,
                           "user_agent": user_agent, "saved_at": time.time()},
                          out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, _CACHE_PATH)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError:
        logger.debug("eastmoney_auth 写缓存失败", exc_info=True)
    finally:
        if handle is not None:
            _unlock(handle)


def _delete_cached() -> None:
    handle = None
    try:
        handle = _lock_file()
        _CACHE_PATH.unlink()
    except (FileNotFoundError, OSError):
        pass
    finally:
        if handle is not None:
            _unlock(handle)


def load_cached() -> bool:
    """把盘上的凭据装进内存。启动时调一次，省掉一次采集。

    盘上那份的年龄按**墙上时钟**算（存的是 ``time.time()``），而运行期的 TTL 判断用
    单调时钟——两者不能混：进程重启后单调时钟从零开始，拿它减出来的年龄永远是 0。
    """
    if not enabled():
        return False
    payload = _read_cached()
    if payload is None:
        return False
    saved_at = payload.get("saved_at")
    if not isinstance(saved_at, (int, float)):
        return False
    age = time.time() - saved_at
    if age < 0:
        return False
    global _harvested_at
    _store(str(payload[COOKIE_NAME]), str(payload.get("user_agent") or ""))
    with _lock:
        # 存盘那一刻起就在走 TTL，装进来时要把已经过掉的那部分补上，否则重启一次
        # 就等于给凭据续了一整个 TTL。
        _harvested_at = _now() - age
    logger.info("eastmoney_auth 从缓存装载凭据 已用=%.0fs%s", age,
                "（已到刷新时间，继续使用并后台刷新）"
                if age >= EASTMONEY_AUTH_TTL_SECONDS else "")
    # 不在装载阶段启动浏览器。load_cached() 发生在数据源 import 期间；首次真正请求
    # 会通过 cookie_header() 继续使用旧值并异步刷新。
    return True


# --- 采集 ---------------------------------------------------------------------


async def _harvest_async() -> tuple:
    """开一个短命浏览器，加载行情列表页，等 JS 写出凭据。

    返回 ``(值, UA)``；拿不到返回 ``("", "")``。
    """
    from playwright.async_api import async_playwright

    deadline = _now() + EASTMONEY_AUTH_HARVEST_TIMEOUT_SECONDS
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context()
            page = await context.new_page()
            remaining = max(1.0, deadline - _now())
            await page.goto(EASTMONEY_AUTH_PAGE, wait_until="domcontentloaded",
                            timeout=remaining * 1000)
            user_agent = ""
            try:
                user_agent = str(await page.evaluate("navigator.userAgent"))
            except Exception:
                pass
            # JS 写 cookie 比 domcontentloaded 晚不少（实测要等到秒级），所以轮询而
            # 不是固定 sleep：拿到就走，省掉剩下的等待。
            while _now() < deadline:
                for cookie in await context.cookies("https://push2.eastmoney.com/"):
                    if cookie.get("name") == COOKIE_NAME and cookie.get("value"):
                        return str(cookie["value"]), user_agent
                await page.wait_for_timeout(500)
            return "", user_agent
        finally:
            await browser.close()


def _harvest_worker() -> None:
    global _harvesting, _last_attempt_at
    value = user_agent = ""
    failed = False
    started = _now()
    try:
        import asyncio

        value, user_agent = asyncio.run(_harvest_async())
    except Exception:
        failed = True
        logger.warning("eastmoney_auth 采集失败，这几个主机继续走不带凭据的老路",
                       exc_info=True)
    try:
        if not value:
            # 采不到就保留手上的值。上游不可用时丢掉旧值只会让后续请求更差。
            if not failed:
                logger.warning(
                    "eastmoney_auth 采集未拿到 %s 耗时=%.1fs（保留手上的凭据）",
                    COOKIE_NAME, _now() - started,
                )
            return
        changed = _store(value, user_agent)
        _write_cached(value, user_agent)
        if changed:
            logger.info("eastmoney_auth 采集到凭据 耗时=%.1fs", _now() - started)
        else:
            logger.info("eastmoney_auth 重采得到同一个值 耗时=%.1fs",
                        _now() - started)
    finally:
        # 写内存和落盘完成后再释放单飞标志，避免极短窗口里启动第二个浏览器采集。
        with _lock:
            _harvesting = False
            _last_attempt_at = _now()


def _spawn(target) -> None:
    """把采集放到后台线程上。

    单独一个函数，是为了给测试一个**只有一处**的拦截点：直接换掉 ``threading.Thread``
    会在用例期间改掉整个进程的线程构造，而这一层只想让自己不起线程。
    """
    threading.Thread(target=target, name="eastmoney-auth-harvest", daemon=True).start()


def ensure_ready() -> bool:
    """手上没有可用凭据时，在后台采一次。**立即返回**。

    返回是否新起了一次采集。同一时刻最多一个在飞；失败之后按 TTL 的十分之一留一个
    重试间隔，否则每个请求都会踢一次采集，把一次上游不可用放大成一串浏览器进程。
    """
    global _harvesting
    if not enabled():
        return False
    with _lock:
        if _harvesting:
            return False
        if _value is not None and not _expired(_harvested_at) and not _suspect:
            return False
        backoff = EASTMONEY_AUTH_TTL_SECONDS / 10.0
        if _last_attempt_at and _now() - _last_attempt_at < backoff:
            return False
        _harvesting = True
    try:
        _spawn(_harvest_worker)
    except Exception:
        # 起不了线程就把标志位放回去，否则这一层从此再也不采。
        with _lock:
            _harvesting = False
        logger.warning("eastmoney_auth 无法启动采集线程", exc_info=True)
        return False
    return True


def state() -> dict:
    """给启动日志和 health 用的一行状态。不含凭据本身。"""
    with _lock:
        return {
            "enabled": enabled(),
            "present": _value is not None,
            "age_seconds": round(_now() - _harvested_at, 1) if _value else None,
            "expired": bool(_value) and _expired(_harvested_at),
            "suspect": _suspect,
            "failures": _failures,
            "harvesting": _harvesting,
            "hosts": len(AUTH_HOSTS),
        }


def describe() -> str:
    current = state()
    if not current["enabled"]:
        return "eastmoney_auth=disabled"
    if not current["present"]:
        return "eastmoney_auth=absent" + (" harvesting" if current["harvesting"] else "")
    state_word = "suspect" if current["suspect"] else "present"
    return f"eastmoney_auth={state_word} age={current['age_seconds']:.0f}s"


def reset() -> None:
    """清掉进程内状态。给测试用，不碰磁盘。"""
    global _value, _harvested_at, _user_agent, _failures, _suspect
    global _harvesting, _last_attempt_at
    with _lock:
        _value = None
        _harvested_at = 0.0
        _user_agent = ""
        _failures = 0
        _suspect = False
        _harvesting = False
        _last_attempt_at = 0.0


__all__ = [
    "AUTH_HOSTS",
    "AUTH_PATHS",
    "COOKIE_NAME",
    "cookie_header",
    "describe",
    "enabled",
    "has_credential",
    "ensure_ready",
    "invalidate",
    "load_cached",
    "needs_auth",
    "note_outcome",
    "remember_cookies",
    "remember_from_context",
    "reset",
    "state",
]
