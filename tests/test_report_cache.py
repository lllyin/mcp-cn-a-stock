"""报告缓存单元测试

覆盖三件事：纪元边界是否与渲染分支对齐、开关是否真的能关掉、
以及开启缓存前后返回结果是否逐字节一致。
"""

import datetime
import pathlib
import importlib
import json
import os
import time
from io import StringIO
from zoneinfo import ZoneInfo

import pytest

from finmcp import cache as cache_module
from finmcp import market_session as ms
from finmcp import config
from finmcp.cache import (
    PHASE_CLOSED,
    PHASE_LIVE,
    PHASE_LUNCH,
    PHASE_POSTCLOSE,
    ReportCache,
    build_key,
    is_cacheable_report,
    market_phase,
)

# 2026-08-17 是周一，2026-08-21 是周五。
MONDAY = datetime.date(2026, 8, 17)
FRIDAY = datetime.date(2026, 8, 21)
SATURDAY = datetime.date(2026, 8, 22)


def at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute))


def moment(clock, day=MONDAY):
    """边界本身的那一刻。不写死时刻——写死就会在下次调边界时又漂一遍。"""
    return datetime.datetime.combine(day, clock)


def before(clock, minutes=1, day=MONDAY):
    return moment(clock, day) - datetime.timedelta(minutes=minutes)


def after(clock, minutes=1, day=MONDAY):
    return moment(clock, day) + datetime.timedelta(minutes=minutes)


def make_cache(tmp_path, **kwargs) -> ReportCache:
    params = {
        "enabled": True,
        "live_ttl_seconds": 60.0,
        "max_entries": 64,
        "disk_enabled": False,
        "directory": str(tmp_path / "cache"),
    }
    params.update(kwargs)
    return ReportCache(**params)


# --- 纪元边界 -----------------------------------------------------------


@pytest.mark.parametrize(
    "moment,expected_phase",
    [
        (at(MONDAY, 9, 14), PHASE_CLOSED),   # 开盘前
        (at(MONDAY, 9, 15), PHASE_LIVE),     # research.is_realtime_fund_flow_window 起点
        (at(MONDAY, 11, 29), PHASE_LIVE),
        (at(MONDAY, 11, 35), PHASE_LUNCH),   # 午休缓冲结束后才完全复用
        (at(MONDAY, 12, 59), PHASE_LUNCH),
        (at(MONDAY, 13, 0), PHASE_LIVE),
        (at(MONDAY, 15, 29), PHASE_LIVE),
        (at(MONDAY, 15, 30), PHASE_POSTCLOSE),
        (before(ms.FINAL_TIME), PHASE_POSTCLOSE),
        (after(ms.EVENING_SETTLE), PHASE_CLOSED),   # 资金流分支翻转 + 缓冲
        (at(SATURDAY, 11, 0), PHASE_CLOSED),
    ],
)
def test_market_phase_boundaries(moment, expected_phase):
    phase, _ = market_phase(moment)
    assert phase == expected_phase


def test_closed_epoch_spans_overnight():
    """周一 18:00 与周二 08:00 属于同一纪元，两者渲染同一个资金流分支。"""
    _, evening = market_phase(at(MONDAY, 18, 0))
    _, next_morning = market_phase(at(MONDAY + datetime.timedelta(days=1), 8, 0))
    assert evening == next_morning


def test_closed_epoch_spans_weekend():
    """周五收盘后到周一开盘前是一个连续纪元。"""
    _, friday_evening = market_phase(at(FRIDAY, 18, 0))
    _, saturday = market_phase(at(SATURDAY, 11, 0))
    _, monday_early = market_phase(at(FRIDAY + datetime.timedelta(days=3), 8, 0))
    assert friday_evening == saturday == monday_early


def test_live_and_lunch_are_different_epochs():
    _, live = market_phase(at(MONDAY, 10, 0))
    _, lunch = market_phase(at(MONDAY, 12, 0))
    assert live != lunch


def test_aware_utc_clock_is_normalized_to_shanghai():
    """Ubuntu hosts commonly use UTC; cache boundaries must remain China time."""
    utc = ZoneInfo("UTC")
    shanghai_open = datetime.datetime(2026, 8, 17, 1, 15, tzinfo=utc)
    shanghai_before_open = datetime.datetime(2026, 8, 17, 1, 0, tzinfo=utc)

    assert market_phase(shanghai_open)[0] == PHASE_LIVE  # 09:15 Shanghai
    assert market_phase(shanghai_before_open)[0] == PHASE_CLOSED  # 09:00 Shanghai


def test_aware_utc_clock_normalizes_cache_window_date():
    utc = ZoneInfo("UTC")
    key = build_key(
        "brief",
        "SH600000",
        {},
        now=datetime.datetime(2026, 8, 17, 16, 30, tzinfo=utc),
    )

    assert key.window == "2026-08-18"  # 00:30 on the next day in Shanghai


# --- 结算缓冲可配 -------------------------------------------------------


def test_live_ttl_default_is_thirty_seconds():
    """盘中 TTL 直接决定用户能拿到多旧的资金流数字，默认值不应被无意改动。

    实测依据：60 秒窗口内主力净流入 P90 相对漂移 21%，30 秒窗口 7.7%；
    代价是约 2.8 个百分点的积分降幅。详见 docs/technical-details.md。
    """
    assert config.CACHE_INTRADAY_TTL_SECONDS == 30.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, datetime.time(15, 30)),      # 默认
        ("1600", datetime.time(16, 0)),
        ("1500", datetime.time(15, 0)),
        ("", datetime.time(15, 30)),        # 空值回退
        ("abcd", datetime.time(15, 30)),    # 非数字回退
        ("99", datetime.time(15, 30)),      # 位数不足回退
        ("1560", datetime.time(15, 30)),    # 分钟越界回退
    ],
)
def test_settle_hhmm_parsing(raw, expected):
    assert config._parse_hhmm(raw, datetime.time(15, 30)) == expected


def test_postclose_and_evening_are_different_epochs():
    """17:00 前后渲染分支不同，绝不能落在同一个纪元。"""
    _, postclose = market_phase(before(ms.FINAL_TIME))
    _, evening = market_phase(at(MONDAY, 18, 0))
    assert postclose != evening


# --- 缓存键 -------------------------------------------------------------


def test_key_splits_at_midnight():
    """load_raw_data 的取数窗口是 now()+1 天，跨零点必须换键。"""
    before = build_key("brief", "SH600000", {}, now=at(MONDAY, 23, 30))
    after = build_key(
        "brief", "SH600000", {}, now=at(MONDAY + datetime.timedelta(days=1), 0, 30)
    )
    assert before.epoch == after.epoch          # 同属一个闭市纪元
    assert before.window != after.window        # 但取数窗口不同
    assert before.digest() != after.digest()


def test_past_dated_query_stays_epoch_bound():
    """任何工具指定历史日期都不享受"已结算"快速通道。

    两个原因：报告类工具即便指定 date 仍打印实时总市值/市盈率
    （_fetch_realtime_sync 不看 date）；而前复权序列是在除权日当天被重算的，
    不是隔夜，所以"当日内可自由复用"会在除权日返回重算前的价格。
    """
    for tool in ("brief", "medium", "full", "tech", "kline_daily", "kline_range"):
        key = build_key(
            tool,
            "SH600000",
            {"date": "2026-08-10"},
            query_date="2026-08-10",
            now=at(MONDAY, 10, 0),
        )
        assert key.phase == PHASE_LIVE, tool
        assert key.epoch == f"live-{MONDAY}", tool


def test_single_epoch_namespace_across_tools():
    """同一时刻所有工具必须落在同一个纪元，否则内存层会互相清空。"""
    now = at(MONDAY, 10, 0)
    epochs = {
        build_key(tool, "SH600000", {"date": "2026-08-10"},
                  query_date="2026-08-10", now=now).epoch
        for tool in ("brief", "full", "tech", "kline_daily", "kline_range")
    }
    assert len(epochs) == 1


def test_mixed_tools_do_not_evict_each_other(tmp_path):
    """回归：kline 与 brief 交替调用时必须都能命中。"""
    c = make_cache(tmp_path)
    now = at(MONDAY, 10, 0)
    k_brief = build_key("brief", "SH600000", {}, now=now)
    k_kline = build_key(
        "kline_daily", "SH600000", {"date": "2026-08-10"},
        query_date="2026-08-10", now=now,
    )
    c.put(k_brief, "BRIEF")
    c.put(k_kline, "KLINE")
    assert c.get(k_brief) == "BRIEF"
    assert c.get(k_kline) == "KLINE"


def test_today_dated_query_stays_epoch_bound():
    """date=今天 仍受盘中波动影响，不能当作已结算。"""
    key = build_key(
        "brief",
        "SH600000",
        {"date": MONDAY.isoformat()},
        query_date=MONDAY.isoformat(),
        now=at(MONDAY, 10, 0),
    )
    assert key.phase == PHASE_LIVE


# --- 边界缓冲窗口 -------------------------------------------------------


@pytest.mark.parametrize(
    "moment,expected_phase",
    [
        (at(MONDAY, 11, 30), PHASE_LIVE),    # 午休刚开始，东财页面尚未定稿
        (at(MONDAY, 11, 34), PHASE_LIVE),
        (at(MONDAY, 11, 35), PHASE_LUNCH),   # 缓冲结束，进入完全复用
        (moment(ms.FINAL_TIME), PHASE_LIVE),   # 分支刚翻转，资金流当日行未必已落
        (before(ms.EVENING_SETTLE), PHASE_LIVE),
        (moment(ms.EVENING_SETTLE), PHASE_CLOSED),
    ],
)
def test_boundary_buffers_delay_full_reuse(moment, expected_phase):
    assert market_phase(moment)[0] == expected_phase


def test_buffer_windows_have_their_own_epoch_tokens():
    """缓冲窗口不能复用 live 的 token，否则会与盘中条目混淆渲染分支。"""
    _, morning_live = market_phase(at(MONDAY, 10, 0))
    _, lunch_buffer = market_phase(at(MONDAY, 11, 31))
    _, lunch_full = market_phase(at(MONDAY, 12, 0))
    _, evening_buffer = market_phase(after(ms.FINAL_TIME))
    _, evening_full = market_phase(at(MONDAY, 18, 0))
    tokens = [morning_live, lunch_buffer, lunch_full, evening_buffer, evening_full]
    assert len(set(tokens)) == len(tokens)


def test_evening_buffer_entry_never_leaks_into_closed_epoch(tmp_path):
    """17:00-17:05 生成的报告（资金流可能还是昨日行）不得流入傍晚纪元。"""
    c = make_cache(tmp_path)
    buffer_key = build_key("brief", "SH600000", {}, now=after(ms.FINAL_TIME))
    c.put(buffer_key, "可能含昨日资金流")
    closed_key = build_key("brief", "SH600000", {}, now=after(ms.EVENING_SETTLE, 25))
    assert c.get(closed_key) is None


def test_key_separates_params_and_symbols():
    base = dict(tool="tech", symbol="SH600000", now=at(MONDAY, 18, 0))
    a = build_key(params={"days": 30}, **base)
    b = build_key(params={"days": 60}, **base)
    c = build_key(tool="tech", symbol="SH600001", params={"days": 30}, now=at(MONDAY, 18, 0))
    assert len({a.digest(), b.digest(), c.digest()}) == 3


# --- 存取与失效 ---------------------------------------------------------


def test_hit_within_same_epoch(tmp_path):
    c = make_cache(tmp_path)
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(key, "报告正文")
    assert c.get(key) == "报告正文"
    assert c.hits == 1


def test_miss_across_epochs(tmp_path):
    c = make_cache(tmp_path)
    evening = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(evening, "收盘后的报告")
    next_live = build_key(
        "brief", "SH600000", {}, now=at(MONDAY + datetime.timedelta(days=1), 10, 0)
    )
    assert c.get(next_live) is None


def test_live_ttl_expires(tmp_path, monkeypatch):
    c = make_cache(tmp_path, live_ttl_seconds=60.0)
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 10, 0))
    assert key.phase == PHASE_LIVE

    now = [1000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: now[0])
    c.put(key, "盘中报告")
    now[0] += 30
    assert c.get(key) == "盘中报告"
    now[0] += 40  # 累计 70s > TTL
    assert c.get(key) is None


def test_live_ttl_zero_disables_intraday_reuse(tmp_path):
    c = make_cache(tmp_path, live_ttl_seconds=0.0)
    live_key = build_key("brief", "SH600000", {}, now=at(MONDAY, 10, 0))
    c.put(live_key, "盘中报告")
    assert c.get(live_key) is None

    # 闭市纪元不受该开关影响
    closed_key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(closed_key, "收盘报告")
    assert c.get(closed_key) == "收盘报告"


def test_master_switch_disables_everything(tmp_path):
    c = make_cache(tmp_path, enabled=False)
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(key, "报告正文")
    assert c.get(key) is None
    assert c.stores == 0


def test_max_entries_is_bounded(tmp_path):
    c = make_cache(tmp_path, max_entries=8)
    for i in range(40):
        key = build_key("brief", f"SH60{i:04d}", {}, now=at(MONDAY, 18, 0))
        c.put(key, "x" * 100)
    assert len(c._entries) <= 8


def test_stale_epoch_entries_are_rejected_on_read(tmp_path):
    """旧纪元条目不再被主动清空（那会导致多命名空间互踢），而是读取时判定失效。"""
    c = make_cache(tmp_path)
    old = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(old, "旧纪元")
    next_day = build_key(
        "brief", "SH600000", {}, now=at(MONDAY + datetime.timedelta(days=1), 10, 0)
    )
    assert c.get(next_day) is None


# --- 瞬时失败不入缓存 ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "# 基本数据\n- [实时抓取失败] timeout",
        "# 基本数据\n- [实时调用异常] boom",
        "# 基本数据\n- 盘中实时数据暂时不可用",
        "Error during processing: connection reset",
        "",
        "   ",
    ],
)
def test_transient_failures_are_not_cacheable(text):
    assert is_cacheable_report(text) is False


def test_normal_report_is_cacheable():
    assert is_cacheable_report("# 基本数据\n- 股票代码: SH600000") is True


# --- 渲染指纹 -----------------------------------------------------------


def test_digest_includes_render_fingerprint(monkeypatch):
    """部署新渲染代码后，磁盘层里的旧输出必须自动失效。

    闭市纪元最长 64 小时，没有指纹的话傍晚上线的渲染修复要到次日开盘才生效。
    """
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    before = key.digest()
    monkeypatch.setattr(cache_module, "RENDER_FINGERPRINT", "deadbeefcafe")
    assert key.digest() != before


def test_render_fingerprint_is_stable_within_a_build():
    assert cache_module._render_fingerprint() == cache_module.RENDER_FINGERPRINT


def test_new_build_cannot_read_old_disk_entry(tmp_path, monkeypatch):
    directory = str(tmp_path / "cache")
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))

    old_build = make_cache(tmp_path, disk_enabled=True, directory=directory)
    old_build.put(key, "上线前的渲染结果")
    assert old_build.get(key) == "上线前的渲染结果"

    monkeypatch.setattr(cache_module, "RENDER_FINGERPRINT", "newbuild1234")
    new_build = make_cache(tmp_path, disk_enabled=True, directory=directory)
    assert new_build.get(key) is None


def test_index_without_fund_flow_page_is_cacheable():
    """指数没有资金流页面是稳定事实，不是瞬时故障。"""
    assert is_cacheable_report("# 基本数据\n- 暂无实时资金流向") is True


# --- 磁盘层 -------------------------------------------------------------


def test_disk_tier_survives_new_instance(tmp_path):
    directory = str(tmp_path / "cache")
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))

    first = make_cache(tmp_path, disk_enabled=True, directory=directory)
    first.put(key, "报告正文")

    second = make_cache(tmp_path, disk_enabled=True, directory=directory)
    assert second.get(key) == "报告正文"


def test_disk_sweep_only_touches_own_directories(tmp_path):
    """回归：清理只删自己建的 epoch- 目录，且必须过了保留期。

    早期实现会 rmtree 掉目录下所有非当前纪元的条目——把 REPORT_CACHE_DIR
    指向任何已有目录都会被清空。
    """
    directory = tmp_path / "cache"
    directory.mkdir()
    stranger = directory / "important-user-data"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("不能被删", encoding="utf-8")

    c = make_cache(tmp_path, disk_enabled=True, directory=str(directory))
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(key, "报告正文")

    c._last_sweep_at = 0.0  # 强制触发一次清理
    c._sweep_disk()

    assert (stranger / "keep.txt").read_text(encoding="utf-8") == "不能被删"
    assert c.get(key) == "报告正文", "保留期内的自有条目不应被清理"


def test_disk_sweep_retires_expired_epoch_dirs(tmp_path):
    directory = tmp_path / "cache"
    c = make_cache(tmp_path, disk_enabled=True, directory=str(directory))
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(key, "报告正文")

    epoch_dir = pathlib.Path(c.directory) / f"{cache_module.EPOCH_DIR_PREFIX}{key.epoch}"
    assert epoch_dir.is_dir()

    expired = time.time() - cache_module.DISK_RETENTION_SECONDS - 60
    os.utime(epoch_dir, (expired, expired))
    c._last_sweep_at = 0.0
    c._sweep_disk()

    assert not epoch_dir.exists()


def test_disk_sweep_is_rate_limited(tmp_path, monkeypatch):
    """清理不能每次请求都跑：早期实现因纪元 token 交替而退化成每请求全目录扫描。"""
    directory = tmp_path / "cache"
    c = make_cache(tmp_path, disk_enabled=True, directory=str(directory))
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(key, "x")

    # cache_module.os 就是 os 本身，这里替换的是进程级的 os.listdir；
    # 必须交给 monkeypatch 托管，否则用例中途抛异常会把桩留给后续用例。
    calls = []
    original = os.listdir

    def spy(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(cache_module.os, "listdir", spy)
    for _ in range(20):
        c.put(key, "x")
        c.get(key)
    assert calls == [], "保留期内不应重复扫描目录"


def test_disk_payload_is_readable_json(tmp_path):
    directory = str(tmp_path / "cache")
    c = make_cache(tmp_path, disk_enabled=True, directory=directory)
    key = build_key("brief", "SH600000", {}, now=at(MONDAY, 18, 0))
    c.put(key, "报告正文")

    # c.directory 比传进去的多一层命名空间子目录：每个域各扫各的，一个域的清扫
    # 不会顺手带走另一个域的条目。
    path = (
        pathlib.Path(c.directory)
        / f"{cache_module.EPOCH_DIR_PREFIX}{key.epoch}"
        / f"{key.digest()}.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["value"] == "报告正文"
    assert payload["epoch"] == key.epoch


# --- 守卫：当日资金流还在路上时，不进 CLOSED 纪元 ------------------------------
#
# CLOSED 纪元长达 16–64 小时，而 AkShare 的当日资金流行要到收盘后一段时间才落地。
# MARKET_EPOCH_FINAL_TIME 配早了这一段就会缺，然后被冻一整晚。守卫把「配早了」的代价
# 从数据缺失降到少命中几次缓存。
#
# 「滞后了没有」由调用方算好传进来，不在缓存层从渲染结果里正则抠日期——抠出来的
# 东西依赖标题措辞，措辞一改守卫就悄悄失效，而失效是看不出来的。


REPORT = "# 基本数据\n- 股票代码: SH600000\n- 数据日期: 2026-09-04\n\n## 资金流向\n- 主力净流入: 1.22亿\n"


def test_a_lagging_fund_flow_is_kept_out_of_the_closed_epoch():
    assert is_cacheable_report(REPORT, phase="closed", fund_flow_lagging=True) is False


def test_a_settled_fund_flow_is_cacheable():
    assert is_cacheable_report(REPORT, phase="closed", fund_flow_lagging=False) is True


def test_a_lagging_fund_flow_is_fine_before_the_epoch_closes():
    """盘中和盘后本来就走短 TTL，不用拦——拦了只是白白少命中几次。"""
    assert is_cacheable_report(REPORT, phase="postclose", fund_flow_lagging=True) is True
    assert is_cacheable_report(REPORT, phase="live", fund_flow_lagging=True) is True


def test_the_guard_is_off_when_the_phase_is_unknown():
    """不传 phase 就是老调用方，行为和以前一致。"""
    assert is_cacheable_report(REPORT) is True


def test_a_transient_failure_still_wins_over_everything():
    """瞬时失败任何纪元都不缓，和滞后与否无关。"""
    broken = REPORT + "- 盘中实时数据暂时不可用\n"
    assert is_cacheable_report(broken, phase="closed", fund_flow_lagging=False) is False
