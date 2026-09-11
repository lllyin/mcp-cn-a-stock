"""东财 nid18 凭据：判断范围、复用周期、失效重采，以及三种通道模式下的注入。

这一层的收益全部落在"请求有没有带上那个 Cookie"上，所以断言基本都是断在请求头上，
而不是断某个内部状态。每组正面用例都配一个**负面对照**：把注入关掉，断言 Cookie
就不见了。没有那个对照，"带上了"这件事可能只是因为别处早就有一个 Cookie。
"""

import importlib
import json
import time

import pytest
import requests as std_requests

auth = importlib.import_module("finmcp.datasource.eastmoney_auth")
channel = importlib.import_module("finmcp.datasource.http_channel")

PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get?pn=1"
PUSH2HIS = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
PUSH2HIS_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
OTHER = "https://d.10jqka.com.cn/v6/line/hs_600519/01/2026.js"


@pytest.fixture(autouse=True)
def harvests(monkeypatch, tmp_path):
    """每个用例从"开着、没有凭据、不会真采集"开始，并交出采集的调用记录。

    两件事都不是洁癖：

    - **缓存路径换掉**。默认指向仓库的 .runtime，跑测试会覆盖当前环境上真实采到的
      那份凭据。
    - **采集拦住**。不拦的话，任何一个"手上没凭据"的用例都会经 ``cookie_header``
      踢一次真采集，起一个 Chromium。实测复现过：跑一次全量测试就在仓库的 .runtime
      里写下了一份真实凭据——后台线程跑完时 monkeypatch 已经退栈，路径又指回去了。
      这也是"测试不该产生副作用"里最难看的一种，因为它连日志都不留。
    """
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "_CACHE_PATH", tmp_path / "eastmoney-auth.json")
    started: list = []
    monkeypatch.setattr(auth, "_spawn", lambda target: started.append(target))
    auth.reset()
    yield started
    auth.reset()


# --- 判断范围 ---------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    (PUSH2, True),
    (PUSH2HIS, True),
    ("https://push2his.eastmoney.com/api/qt/stock/kline/get", True),
    ("https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519", True),
    # 页面资源正常放行，带凭据没有意义。和 _is_impersonated 同一判断。
    ("https://push2.eastmoney.com/x.js", False),
    ("https://push2.eastmoney.com/x.html", False),
    # 实测带与不带都是 6/6 的主机不进这一层：影响面不能比证据大。
    ("https://fund.eastmoney.com/js/fundcode_search.js", False),
    ("https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax", False),
    (OTHER, False),
    # 查询参数里恰好出现受管主机名，不该把一个打同花顺的请求算进来。
    ("https://q.10jqka.com.cn/x?ref=push2.eastmoney.com", False),
    ("", False),
    (None, False),
])
def test_needs_auth_scope(url, expected):
    assert auth.needs_auth(url) is expected


def test_needs_auth_survives_malformed_url():
    """URL 解析不了也只能返回 False，不能把异常抛进请求路径。"""
    assert auth.needs_auth("http://[oops") is False


def test_disabled_means_no_scope_at_all(monkeypatch):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_ENABLED", False)
    assert auth.needs_auth(PUSH2) is False
    assert auth.cookie_header(PUSH2) == ""


# --- 取值与复用 -------------------------------------------------------------


def test_cookie_header_returns_value_once_present():
    auth._store("abc123")
    assert auth.cookie_header(PUSH2) == "nid18=abc123"
    assert auth.cookie_header(PUSH2HIS) == "nid18=abc123"
    # 负面对照：不在范围里的主机拿不到，说明上面那两个不是无条件返回。
    assert auth.cookie_header(OTHER) == ""


def test_has_credential_includes_an_expired_value_waiting_for_refresh(monkeypatch):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 1.0)
    auth._store("old")
    auth._harvested_at = time.monotonic() - 2.0
    assert auth.has_credential() is True
    auth.reset()
    assert auth.has_credential() is False


def test_eastmoney_platform_is_not_preemptively_skipped_when_auth_is_present(monkeypatch):
    from finmcp.datasource.platforms import eastmoney

    monkeypatch.setattr(channel, "impersonated_hosts_degraded", lambda: True)
    platform = eastmoney.EastmoneyPlatform()
    assert platform.degraded() is True
    auth._store("signed")
    assert platform.degraded() is False


def test_cookie_header_never_harvests_inline(monkeypatch):
    """请求路径上不许等采集。没有值就返回空串，同时把采集踢到后台。"""
    kicked = []
    monkeypatch.setattr(auth, "ensure_ready", lambda: kicked.append(True) or True)
    start = time.monotonic()
    assert auth.cookie_header(PUSH2) == ""
    assert time.monotonic() - start < 0.5
    assert kicked == [True]


def test_expired_value_is_used_while_refresh_starts(monkeypatch):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 100.0)
    auth._store("abc123")
    assert auth.cookie_header(PUSH2) == "nid18=abc123"
    # 把采集时刻推到 TTL 之外，不碰值本身。
    auth._harvested_at = time.monotonic() - 101.0
    kicked = []
    monkeypatch.setattr(auth, "ensure_ready", lambda: kicked.append(True) or True)
    assert auth.cookie_header(PUSH2) == "nid18=abc123"
    assert kicked == [True]
    # TTL 只触发刷新，旧值在新值到手前继续使用。
    assert auth.state()["present"] is True
    assert auth.state()["expired"] is True


# --- 失效与重采 -------------------------------------------------------------


def test_single_failure_does_not_discard_a_good_value(monkeypatch):
    """这几个端点带着有效凭据也会偶尔空手，按单次判会把好值换掉。"""
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES", 3)
    monkeypatch.setattr(auth, "ensure_ready", lambda: False)
    auth._store("good")
    auth.note_outcome(PUSH2, success=False)
    auth.note_outcome(PUSH2, success=False)
    assert auth.cookie_header(PUSH2) == "nid18=good"


def test_consecutive_failures_rekick_but_keep_using_the_value(monkeypatch, harvests):
    """上游进"谁都取不到"的窗口时，丢掉凭据会让服务比改动前更差。

    实测踩到过：带真凭据和不带都是 0/6 的窗口里，"连拒就丢"把一份完好的凭据删了，
    于是窗口期内连 Cookie 都不带。所以到阈值只标记可疑并去重采，手上那个照旧用。
    """
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES", 3)
    auth._store("stillgood")
    for _ in range(3):
        auth.note_outcome(PUSH2, success=False)
    assert auth.cookie_header(PUSH2) == "nid18=stillgood"
    assert auth.state()["suspect"] is True
    assert len(harvests) == 1


def test_suspicion_does_not_re_announce_every_round(monkeypatch, harvests):
    """已经在等重采了就别再喊。否则一次长时间不可用会刷满 WARNING 并反复踢采集。"""
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES", 2)
    auth._store("v")
    for _ in range(10):
        auth.note_outcome(PUSH2, success=False)
    assert len(harvests) == 1


def test_a_success_clears_the_suspicion(monkeypatch, harvests):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES", 2)
    auth._store("v")
    auth.note_outcome(PUSH2, success=False)
    auth.note_outcome(PUSH2, success=False)
    assert auth.state()["suspect"] is True
    auth.note_outcome(PUSH2, success=True)
    assert auth.state()["suspect"] is False


def test_a_failed_reharvest_keeps_the_old_value(monkeypatch):
    """采不到就保留手上那个：可能过期的凭据也强过没有（不带实测 0/6）。"""
    async def nothing():
        return "", ""

    monkeypatch.setattr(auth, "_harvest_async", nothing)
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._store("previous")
    auth._harvesting = True
    auth._harvest_worker()
    assert auth.cookie_header(PUSH2) == "nid18=previous"


def test_a_reharvest_that_returns_the_same_value_clears_suspicion(monkeypatch):
    """值是确定性的，重采多半拿回同一个——但那次页面加载已经把它重新登记了。

    所以"没换"也算处理完了。还挂着可疑的话，每一轮失败都会再踢一次采集。
    """
    async def same():
        return "deterministic", ""

    monkeypatch.setattr(auth, "_harvest_async", same)
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._store("deterministic")
    auth._suspect = True
    auth._harvesting = True
    auth._harvest_worker()
    assert auth.state()["suspect"] is False
    assert auth.cookie_header(PUSH2) == "nid18=deterministic"


def test_a_suspect_value_is_allowed_to_be_reharvested(monkeypatch, harvests):
    """可疑时即使没过期也要放行采集，否则标记了也没人去查。"""
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._store("v")
    assert auth.ensure_ready() is False        # 没可疑，不采
    auth._suspect = True
    assert auth.ensure_ready() is True
    assert len(harvests) == 1


def test_success_resets_the_failure_run(monkeypatch):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES", 3)
    monkeypatch.setattr(auth, "ensure_ready", lambda: False)
    auth._store("good")
    auth.note_outcome(PUSH2, success=False)
    auth.note_outcome(PUSH2, success=False)
    auth.note_outcome(PUSH2, success=True)
    auth.note_outcome(PUSH2, success=False)
    auth.note_outcome(PUSH2, success=False)
    # 连续计数被中间那次成功清零，所以到这里还没到阈值。
    assert auth.cookie_header(PUSH2) == "nid18=good"


def test_failures_on_other_hosts_are_not_counted(monkeypatch):
    """别的主机失败跟这个凭据无关，不该把它记成凭据的账。"""
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES", 2)
    monkeypatch.setattr(auth, "ensure_ready", lambda: False)
    auth._store("good")
    for _ in range(5):
        auth.note_outcome(OTHER, success=False)
    assert auth.cookie_header(PUSH2) == "nid18=good"


def test_note_outcome_without_a_value_is_a_noop(monkeypatch):
    monkeypatch.setattr(auth, "ensure_ready", lambda: False)
    auth.note_outcome(PUSH2, success=False)
    assert auth.state()["failures"] == 0


def test_invalidate_only_drops_the_value_it_was_given():
    """并发下别踩掉另一个线程刚采到的新值。"""
    auth._store("old")
    auth._store("new")
    auth.invalidate("old")
    assert auth.cookie_header(PUSH2) == "nid18=new"
    auth.invalidate("new")
    assert auth.cookie_header(PUSH2) == ""


# --- 顺手采 -----------------------------------------------------------------


def test_remember_picks_the_credential_out_of_a_cookie_list():
    assert auth.remember_cookies([
        {"name": "qgqp_b_id", "value": "x"},
        {"name": "nid18", "value": "harvested"},
        {"name": "st_si", "value": "y"},
    ]) is True
    assert auth.cookie_header(PUSH2) == "nid18=harvested"


def test_remember_does_not_fsync_the_same_value_on_every_page(monkeypatch):
    writes = []
    monkeypatch.setattr(auth, "_write_cached", lambda value, ua: writes.append(value))
    cookies = [{"name": "nid18", "value": "same"}]
    assert auth.remember_cookies(cookies) is True
    assert auth.remember_cookies(cookies) is True
    assert writes == ["same"]


@pytest.mark.parametrize("cookies", [
    [],
    None,
    [{"name": "qgqp_b_id", "value": "x"}],
    [{"name": "nid18", "value": ""}],
])
def test_remember_ignores_lists_without_a_usable_credential(cookies):
    assert auth.remember_cookies(cookies) is False
    assert auth.cookie_header(PUSH2) == ""


def test_remember_survives_a_garbage_list():
    """调用点在取数路径上：采凭据失败不能让那次取数失败。"""
    assert auth.remember_cookies(["not a dict", 42, None]) is False


# --- 磁盘缓存 ---------------------------------------------------------------


def test_cached_credential_survives_a_restart(monkeypatch):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._write_cached("persisted", "UA/1.0")
    auth.reset()
    assert auth.load_cached() is True
    assert auth.cookie_header(PUSH2) == "nid18=persisted"


def test_restart_does_not_renew_the_ttl(monkeypatch):
    """存盘那一刻起就在走 TTL。装载时要把已经过掉的补上。

    不补的话，每次重启都等于给凭据续一个整 TTL——一个早该重采的值可以靠反复重启
    无限延寿，而它对应的服务端登记早就没了。
    """
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._write_cached("aged", "")
    # 手改盘上的时间戳，装作是 3000 秒前存的。
    payload = json.loads((auth._CACHE_PATH).read_text(encoding="utf-8"))
    payload["saved_at"] = time.time() - 3000.0
    auth._CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")

    auth.reset()
    assert auth.load_cached() is True
    assert 2900 < auth.state()["age_seconds"] < 3100


def test_cached_credential_past_its_ttl_is_used_while_refresh_starts(monkeypatch, harvests):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 100.0)
    auth._write_cached("tooold", "")
    payload = json.loads((auth._CACHE_PATH).read_text(encoding="utf-8"))
    payload["saved_at"] = time.time() - 101.0
    auth._CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    auth.reset()
    assert auth.load_cached() is True
    assert auth.cookie_header(PUSH2) == "nid18=tooold"
    assert len(harvests) == 1
    assert auth.state()["expired"] is True


@pytest.mark.parametrize("payload", [
    "{ not json",
    json.dumps({"version": 999, "nid18": "x", "saved_at": 0}),
    json.dumps({"version": 1, "saved_at": 0}),                     # 缺凭据
    json.dumps({"version": 1, "nid18": "x"}),                      # 缺时间戳
    json.dumps({"version": 1, "nid18": "x", "saved_at": "昨天"}),  # 时间戳不是数
    json.dumps(["not", "a", "dict"]),
])
def test_unreadable_cache_is_refused_not_crashed(payload):
    auth._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth._CACHE_PATH.write_text(payload, encoding="utf-8")
    assert auth.load_cached() is False


def test_cache_is_written_with_owner_only_permissions():
    auth._write_cached("secret", "")
    assert oct(auth._CACHE_PATH.stat().st_mode & 0o777) == "0o600"


def test_invalidate_removes_the_cached_copy(monkeypatch):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._write_cached("gone", "")
    auth._store("gone")
    auth.invalidate("gone")
    assert auth._CACHE_PATH.exists() is False
    # 负面对照：不删的话重启就把刚判定失效的值又装回来。
    assert auth.load_cached() is False


def test_missing_cache_file_is_not_an_error():
    assert auth.load_cached() is False
    auth.invalidate()  # 删一个不存在的文件


def test_tests_never_touch_the_real_credential_path(tmp_path):
    """守住上面那个 fixture 的两条隔离，别让下一个人不小心把它拿掉。

    这两条一旦失效，症状是"跑测试会静默改掉当前环境的凭据、并起一个浏览器"，
    而不是某个用例红——所以要有一条用例专门盯着它。
    """
    assert auth._CACHE_PATH.parent == tmp_path
    assert auth._spawn.__name__ == "<lambda>"


# --- 采集的调度 -------------------------------------------------------------


def test_ensure_ready_starts_at_most_one_harvest(harvests):
    """采集要开浏览器，而请求路径是并发的。一个都不拦就是一串浏览器进程。"""
    assert auth.ensure_ready() is True
    assert auth.ensure_ready() is False
    assert auth.ensure_ready() is False
    assert len(harvests) == 1


def test_ensure_ready_does_nothing_when_a_fresh_value_is_held(monkeypatch, harvests):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._store("fresh")
    assert auth.ensure_ready() is False
    assert harvests == []


def test_ensure_ready_backs_off_after_a_failed_harvest(monkeypatch, harvests):
    """上游不可用时，别把一次失败放大成每个请求一个浏览器进程。"""
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    assert auth.ensure_ready() is True
    auth._harvesting = False
    auth._last_attempt_at = time.monotonic()      # 刚失败过
    assert auth.ensure_ready() is False
    # 退避窗口过去之后可以再采。
    auth._last_attempt_at = time.monotonic() - (3600.0 / 10.0) - 1
    assert auth.ensure_ready() is True
    assert len(harvests) == 2


def test_ensure_ready_is_off_when_disabled(monkeypatch, harvests):
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_ENABLED", False)
    assert auth.ensure_ready() is False
    assert harvests == []


def test_a_thread_that_will_not_start_does_not_wedge_the_flag(monkeypatch):
    """起不了线程就得把标志位放回去，否则这一层从此再也不采。"""
    monkeypatch.setattr(auth, "_spawn", lambda target: (_ for _ in ()).throw(
        RuntimeError("注入：起不了线程")))
    assert auth.ensure_ready() is False
    assert auth.state()["harvesting"] is False


def test_harvest_failure_leaves_no_credential_and_clears_the_flag(monkeypatch):
    """采集炸了也只能是"没有凭据"，不能把标志位卡住让之后再也不采。"""
    def boom():
        raise RuntimeError("注入：浏览器起不来")

    monkeypatch.setattr(auth, "_harvest_async", boom)
    auth._harvesting = True
    auth._harvest_worker()
    assert auth.state()["present"] is False
    assert auth.state()["harvesting"] is False


def test_harvest_stores_and_persists_what_it_got(monkeypatch):
    async def fake():
        return "picked", "UA/9"

    monkeypatch.setattr(auth, "_harvest_async", fake)
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_TTL_SECONDS", 3600.0)
    auth._harvesting = True
    auth._harvest_worker()
    assert auth.cookie_header(PUSH2) == "nid18=picked"
    auth.reset()
    assert auth.load_cached() is True       # 落盘了，重启不必重采


def test_describe_never_leaks_the_credential():
    auth._store("s3cret")
    assert "s3cret" not in auth.describe()
    assert "s3cret" not in json.dumps(auth.state())


# --- 通道注入 ---------------------------------------------------------------


@pytest.fixture
def spy_session(monkeypatch):
    """记下最终发出去的 headers，并让 requests 不真的出网。"""
    seen = []

    class Response:
        status_code = 200

    def fake_request(self, method, url, **kwargs):
        seen.append((url, dict(kwargs.get("headers") or {})))
        return Response()

    monkeypatch.setattr(std_requests.Session, "request", fake_request)
    return seen


@pytest.fixture(autouse=True)
def restore_channel(monkeypatch):
    monkeypatch.delattr(std_requests, channel._PROXY_PATCH_MARKER, raising=False)
    before = (std_requests.Session, std_requests.get,
              std_requests.post, std_requests.request)
    yield
    channel.uninstall_http_channel()
    assert (std_requests.Session, std_requests.get,
            std_requests.post, std_requests.request) == before


def _cookie_of(seen, url_fragment):
    for url, headers in seen:
        if url_fragment in url:
            return headers.get("Cookie")
    return None


def test_direct_mode_carries_the_credential(spy_session):
    auth._store("wired")
    channel.install_http_channel("direct")
    std_requests.Session().request("GET", PUSH2)
    std_requests.Session().request("GET", OTHER)
    assert _cookie_of(spy_session, "push2.eastmoney.com") == "nid18=wired"
    # 负面对照：不在范围里的主机没有被顺带加上 Cookie。
    assert _cookie_of(spy_session, "10jqka") is None


def test_direct_mode_carries_the_credential_on_kline(spy_session):
    auth._store("wired")
    channel.install_http_channel("direct")
    std_requests.get(PUSH2HIS_KLINE)
    assert _cookie_of(spy_session, "/api/qt/stock/kline/get") == "nid18=wired"


def test_module_level_helpers_carry_it_too(spy_session):
    """efinance 和 AkShare 都用 requests.get，不只是 Session。"""
    auth._store("wired")
    channel.install_http_channel("direct")
    std_requests.get(PUSH2)
    assert _cookie_of(spy_session, "push2.eastmoney.com") == "nid18=wired"


def test_no_credential_means_no_header(spy_session):
    """负面对照：手上没有值时不能凭空造一个——伪造值实测 1/6，不如不带。"""
    channel.install_http_channel("direct")
    std_requests.Session().request("GET", PUSH2)
    assert _cookie_of(spy_session, "push2.eastmoney.com") is None


def test_disabled_leaves_requests_untouched(monkeypatch):
    """``direct`` 的契约是"原生 requests"。关掉这一层就该一个字节都不改。"""
    monkeypatch.setattr(auth, "EASTMONEY_AUTH_ENABLED", False)
    before = std_requests.Session
    channel.install_http_channel("direct")
    assert std_requests.Session is before


def test_proxy_mode_is_not_wrapped_by_the_auth_layer(monkeypatch):
    """第三方网关完整接管 requests，本层不在它外面再套一层。"""
    installed = []
    monkeypatch.setattr(channel, "_install_proxy",
                        lambda gateway, token, retry: installed.append(gateway))
    monkeypatch.setattr(channel, "_install_auth_cookies",
                        lambda: pytest.fail("proxy 模式不该安装凭据包装", pytrace=False))
    monkeypatch.setattr(auth, "load_cached",
                        lambda: pytest.fail("proxy 模式不该读取本地凭据", pytrace=False))
    assert channel.install_http_channel(
        "proxy", proxy_enabled=True, proxy_gateway="gateway.example"
    ) == "proxy"
    assert installed == ["gateway.example"]
    assert "eastmoney_auth=proxy_managed" in channel.describe_installed_channel()


def test_caller_supplied_cookie_is_not_clobbered(spy_session):
    auth._store("ours")
    channel.install_http_channel("direct")
    std_requests.Session().request("GET", PUSH2, headers={"cookie": "theirs=1"})
    assert _cookie_of(spy_session, "push2") is None      # 大小写不同的那个键
    assert dict(spy_session[0][1])["cookie"] == "theirs=1"


def test_caller_supplied_cookie_does_not_change_our_failure_count(monkeypatch):
    """调用方自带 Cookie 时，请求结果不能被算到本层凭据头上。"""
    auth._store("ours")
    monkeypatch.setattr(std_requests.Session, "request",
                        lambda self, method, url, **kw: (_ for _ in ()).throw(
                            std_requests.ConnectionError("调用方的 Cookie 被拒")))
    channel.install_http_channel("direct")
    with pytest.raises(std_requests.ConnectionError):
        std_requests.Session().request("GET", PUSH2, headers={"Cookie": "theirs=1"})
    assert auth.state()["failures"] == 0


def test_other_headers_are_preserved(spy_session):
    auth._store("ours")
    channel.install_http_channel("direct")
    std_requests.Session().request("GET", PUSH2, headers={"Referer": "https://x/"})
    _, headers = spy_session[0]
    assert headers["Referer"] == "https://x/"
    assert headers["Cookie"] == "nid18=ours"


def test_impersonate_mode_carries_it_on_both_branches(monkeypatch):
    """伪装分支和裸重放分支是同一批主机的两条出路，只给一条带等于另一条继续被拒。"""
    pytest.importorskip("curl_cffi")
    auth._store("wired")
    cffi_seen, plain_seen = [], []

    class Response:
        status_code = 200

    monkeypatch.setattr(channel, "_cffi_session", lambda profile: type(
        "S", (), {"request": lambda self, method, url, **kw:
                  cffi_seen.append(dict(kw.get("headers") or {})) or Response()})())
    assert channel._install_impersonate() is True
    std_requests.Session().request("GET", PUSH2)
    assert cffi_seen and cffi_seen[0].get("Cookie") == "nid18=wired"

    # 让伪装那条全败，逼它走裸重放，看重放有没有带上。
    cffi_seen.clear()

    def explode(self, method, url, **kwargs):
        plain_seen.append(dict(kwargs.get("headers") or {}))
        return Response()

    monkeypatch.setattr(channel, "_cffi_session",
                        lambda profile: type("S", (), {
                            "request": lambda self, m, u, **kw: (_ for _ in ()).throw(
                                RuntimeError("注入：伪装通道不可用"))})())
    monkeypatch.setattr(channel, "_record_impersonation", lambda **kw: None)
    monkeypatch.setattr(std_requests, "_qtf_original_session", std_requests.Session)
    base = channel._restore["Session"]
    monkeypatch.setattr(base, "request", explode)
    std_requests.Session().request("GET", PUSH2)
    assert plain_seen and plain_seen[0].get("Cookie") == "nid18=wired"


def test_plain_replay_success_resets_the_auth_failure_run(monkeypatch):
    """伪装失败、普通请求成功是一整次成功请求，不能触发凭据重采。"""
    pytest.importorskip("curl_cffi")
    auth._store("wired")
    auth._failures = 2

    class Response:
        status_code = 200

    monkeypatch.setattr(channel, "_cffi_session",
                        lambda profile: type("S", (), {
                            "request": lambda self, m, u, **kw: (_ for _ in ()).throw(
                                RuntimeError("注入：伪装通道不可用"))})())
    monkeypatch.setattr(channel, "_record_impersonation", lambda **kw: None)
    assert channel._install_impersonate() is True
    base = channel._restore["Session"]
    monkeypatch.setattr(base, "request", lambda self, method, url, **kw: Response())

    assert std_requests.Session().request("GET", PUSH2).status_code == 200
    assert auth.state()["failures"] == 0


def test_impersonate_bookkeeping_failure_cannot_break_success(monkeypatch):
    pytest.importorskip("curl_cffi")
    auth._store("wired")

    class Response:
        status_code = 200

    monkeypatch.setattr(channel, "_cffi_session", lambda profile: type(
        "S", (), {"request": lambda self, method, url, **kw: Response()})())
    assert channel._install_impersonate() is True
    monkeypatch.setattr(auth, "note_outcome",
                        lambda url, **kw: (_ for _ in ()).throw(
                            RuntimeError("注入：记账写坏了")))
    assert std_requests.Session().request("GET", PUSH2).status_code == 200


def test_a_response_without_a_status_code_does_not_break_the_request(monkeypatch):
    """这一层会包住别人的 Session，"响应"未必是 requests 的 Response。

    记账取不到状态码就该不记这一笔，而不是崩在记账上——把一次本来成功的请求弄崩。
    """
    auth._store("wired")
    monkeypatch.setattr(std_requests.Session, "request",
                        lambda self, method, url, **kw: "passthrough")
    channel.install_http_channel("direct")
    assert std_requests.Session().request("GET", PUSH2) == "passthrough"


def test_bookkeeping_failure_cannot_break_a_working_request(monkeypatch):
    """注入一次已知故障：记账抛异常，请求仍须照常返回。"""
    auth._store("wired")

    class Response:
        status_code = 200

    monkeypatch.setattr(std_requests.Session, "request",
                        lambda self, method, url, **kw: Response())
    channel.install_http_channel("direct")
    monkeypatch.setattr(auth, "note_outcome",
                        lambda url, **kw: (_ for _ in ()).throw(
                            RuntimeError("注入：记账写坏了")))
    assert std_requests.Session().request("GET", PUSH2).status_code == 200


def test_transport_failure_is_still_raised(monkeypatch):
    """记账不能把异常吃掉：调用方要看见 requests 自己的异常类型。"""
    auth._store("wired")
    monkeypatch.setattr(std_requests.Session, "request",
                        lambda self, method, url, **kw: (_ for _ in ()).throw(
                            std_requests.ConnectionError("断连")))
    channel.install_http_channel("direct")
    with pytest.raises(std_requests.ConnectionError):
        std_requests.Session().request("GET", PUSH2)
    assert auth.state()["failures"] == 1


def test_channel_banner_reports_credential_presence():
    auth._store("wired")
    channel.install_http_channel("direct")
    assert "eastmoney_auth=present" in channel.describe_installed_channel()
