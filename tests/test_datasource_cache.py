"""数据源层的 `fund_flow` / `realtime` 缓存（docs/cache-design.md §4.7）。

收编的理由是跨工具重复取数：`brief` / `medium` / `full` 是三个不同的**报告**缓存键，
底下却用同一份资金流和同一份基本数据。2026-09-06 实测 22 次调用里，
资金流取了 56 次只涉及 23 个标的（2.4×），基本数据 68 次 / 24 个标的（2.8×），
其中 44 次资金流走了浏览器兜底、只涉及 22 个标的——一半的页面加载是重复的，
每次 P50 6.4s。

这里最要紧的是**行数守卫**：页面兜底只给 120 行，主源给全部历史。缓存了少的那份
之后不能让要得多的请求命中它，否则缓存把数据变少了（AGENTS §一）。
"""

import pandas as pd
import pytest

from finmcp import cache
from finmcp.datasource import cn_stock_source as css


def _frame(rows: int) -> pd.DataFrame:
    return pd.DataFrame({
        "日期": pd.date_range("2026-01-01", periods=rows).astype(str),
        "主力净流入-净额": [float(i) for i in range(rows)],
    })


def _flow(rows: int, is_market: bool = False) -> dict:
    return {"fund_flow": _frame(rows), "is_market": is_market}


@pytest.fixture(autouse=True)
def enabled_namespaces():
    """conftest 默认把跟市场走的命名空间整层关掉；验缓存本身的测试自己打开。"""
    for name in ("fund_flow", "realtime"):
        target = cache.cache_for(name)
        target.clear()
        target.enabled = True
    yield
    for name in ("fund_flow", "realtime"):
        target = cache.cache_for(name)
        target.clear()
        target.enabled = False


class TestFundFlowRowGuard:
    def test_a_cached_frame_serves_a_request_that_needs_fewer_rows(self):
        calls = []

        def loader():
            calls.append(1)
            return _flow(120)

        first = css._fund_flow_from_cache("SH600519", 60, loader)
        second = css._fund_flow_from_cache("SH600519", 15, loader)

        assert len(first["fund_flow"]) == 120
        assert len(second["fund_flow"]) == 120
        assert len(calls) == 1, "第二次该命中缓存，不该再打上游"

    def test_a_short_cached_frame_does_not_serve_a_request_that_needs_more(self):
        """页面兜底 120 行被缓存后，要 200 行的请求必须重新打上游。

        不守这条就是"缓存让数据变少"——同一个调用，有缓存时拿到 120 行、
        没缓存时拿到 300 行。
        """
        served = [_flow(120), _flow(300)]

        def loader():
            return served.pop(0)

        assert len(css._fund_flow_from_cache("SH600519", 60, loader)["fund_flow"]) == 120
        again = css._fund_flow_from_cache("SH600519", 200, loader)
        assert served == [], "第二次确实打了上游"
        # 契约是"给够本次要的"，不是"原样透传"：存多少由 max(上限, 本次所需) 决定
        assert len(again["fund_flow"]) >= 200

    def test_a_request_beyond_the_cap_still_gets_everything_it_renders(self):
        """fund_flow_limit 超过 250 时，不能被上限砍成 250——那就是缓存让数据变少。"""
        result = css._fund_flow_from_cache("SH600519", 400, lambda: _flow(1200))
        assert len(result["fund_flow"]) >= 400

    def test_the_refetched_frame_replaces_the_short_one(self):
        served = [_flow(120), _flow(300)]
        css._fund_flow_from_cache("SH600519", 60, lambda: served.pop(0))
        css._fund_flow_from_cache("SH600519", 200, lambda: served.pop(0))

        def must_not_run():
            raise AssertionError("覆盖之后 200 行的请求该命中缓存")

        assert len(
            css._fund_flow_from_cache("SH600519", 200, must_not_run)["fund_flow"]
        ) >= 200

    def test_symbols_do_not_share_an_entry(self):
        css._fund_flow_from_cache("SH600519", 15, lambda: _flow(120))
        calls = []
        css._fund_flow_from_cache("SZ000001", 15,
                                  lambda: (calls.append(1), _flow(120))[1])
        assert len(calls) == 1


class TestFundFlowTruncation:
    def test_a_long_history_is_trimmed_before_it_is_stored(self):
        """主源 lmt=0 给全部历史，一条 1200 行就 182 KiB，不截断内存不可控。"""
        trimmed = css._truncate_fund_flow(_flow(1200), css.CACHE_FUND_FLOW_MAX_ROWS)
        assert len(trimmed["fund_flow"]) == css.CACHE_FUND_FLOW_MAX_ROWS

    def test_trimming_keeps_the_newest_rows(self):
        """切的是老的那头。资金流表是旧在前新在后，切错方向就丢了最近的数据。"""
        trimmed = css._truncate_fund_flow(_flow(1200), css.CACHE_FUND_FLOW_MAX_ROWS)
        assert trimmed["fund_flow"]["主力净流入-净额"].iloc[-1] == 1199.0

    def test_a_short_frame_is_untouched(self):
        value = _flow(120)
        assert css._truncate_fund_flow(value, css.CACHE_FUND_FLOW_MAX_ROWS) is value

    def test_failures_pass_through_untouched(self):
        failure = css._fetch_failure("fund_flow")
        assert css._truncate_fund_flow(failure, css.CACHE_FUND_FLOW_MAX_ROWS) is failure

    def test_other_fields_survive_the_trim(self):
        trimmed = css._truncate_fund_flow(_flow(1200, is_market=True), css.CACHE_FUND_FLOW_MAX_ROWS)
        assert trimmed["is_market"] is True


class TestWhatNeverGetsCached:
    """一个纪元长达 64 小时，腌一次瞬时失败就是整个周末没有资金流。"""

    def test_a_failed_fetch_is_not_stored(self):
        calls = []

        def failing():
            calls.append(1)
            return css._fetch_failure("fund_flow")

        css._fund_flow_from_cache("SH600519", 15, failing)
        css._fund_flow_from_cache("SH600519", 15, failing)
        assert len(calls) == 2, "失败不该被缓存，第二次必须重试"

    def test_an_empty_frame_is_not_stored(self):
        calls = []

        def empty():
            calls.append(1)
            return {"fund_flow": pd.DataFrame(), "is_market": False}

        css._fund_flow_from_cache("SH600519", 15, empty)
        css._fund_flow_from_cache("SH600519", 15, empty)
        assert len(calls) == 2

    def test_page_fallback_results_are_written_back(self):
        """兜底不走 get_or_load，靠调用方显式写回；不写回就是每次都重付 6.4 秒。"""
        css.store_fund_flow("SH600519", _flow(120))

        def must_not_run():
            raise AssertionError("写回之后该命中")

        assert css._fund_flow_from_cache("SH600519", 15, must_not_run) is not None

    def test_a_failed_page_fallback_is_not_written_back(self):
        css.store_fund_flow("SH600519", css._fetch_failure("fund_flow"))
        calls = []
        css._fund_flow_from_cache("SH600519", 15,
                                  lambda: (calls.append(1), _flow(120))[1])
        assert len(calls) == 1


class TestRealtimeCache:
    def test_repeated_symbols_hit(self):
        calls = []

        def loader():
            calls.append(1)
            return {"info": {"股票简称": "贵州茅台", "最新价": 1500.0}}

        css._fetch_realtime_from_cache("SH600519", loader)
        css._fetch_realtime_from_cache("SH600519", loader)
        assert len(calls) == 1

    def test_a_failed_lookup_is_not_stored(self):
        calls = []

        def failing():
            calls.append(1)
            return css._fetch_failure("realtime")

        css._fetch_realtime_from_cache("SH600519", failing)
        css._fetch_realtime_from_cache("SH600519", failing)
        assert len(calls) == 2

    def test_a_missing_pb_is_still_cached(self):
        """市净率=无 是记录在案的正常状态（basic_info.py），不是降级，照常缓。

        缓存反而消掉了它的抖动：SH600118 连查 8 次会得到 7 次 10.63、1 次 10.64
        （东财 f167 与本地回退两个口径），缓存之后一个纪元内只取一次。
        """
        value = {"info": {"股票简称": "中国卫星", "最新价": 57.62, "总市值": 6.8e10}}
        assert cache.namespace("realtime").cacheable(value, "SH600118") is True


class TestDiskCodecRoundTrip:
    """磁盘层要把 DataFrame 编成 JSON 再读回来，这是这次改动真正引入的风险。

    活跑的 A/B 证明不了逐字等价：上游会在两次运行之间翻源（主源 API 算出
    `-0.00%`，页面兜底给的是格式化过的 `0.00%`），差异来自源不是来自缓存。
    编解码往返是确定性的，能钉死。
    """

    def test_a_frame_survives_encode_decode_unchanged(self):
        value = _flow(120, is_market=True)
        restored = cache._decode_fund_flow(cache._encode_fund_flow(value))
        pd.testing.assert_frame_equal(
            restored["fund_flow"], value["fund_flow"], check_dtype=False
        )
        assert restored["is_market"] is True

    def test_negative_zero_survives(self):
        """`-0.00%` 和 `0.00%` 在报告里是两个字符串，符号不能在往返中丢。"""
        value = {"fund_flow": pd.DataFrame({"小单净流入-净占比": [-0.0, 0.0, -1.5]}),
                 "is_market": False}
        restored = cache._decode_fund_flow(cache._encode_fund_flow(value))
        got = list(restored["fund_flow"]["小单净流入-净占比"])
        import math
        assert math.copysign(1, got[0]) == -1.0, "负零的符号丢了"
        assert math.copysign(1, got[1]) == 1.0
        assert got[2] == -1.5

    def test_an_empty_frame_round_trips_without_blowing_up(self):
        restored = cache._decode_fund_flow(
            cache._encode_fund_flow({"fund_flow": pd.DataFrame(), "is_market": False}))
        assert restored["fund_flow"].empty

    def test_the_encoded_payload_is_actually_json_serialisable(self):
        """磁盘层的 json.dump 没有 default=，编码结果必须已经是 JSON 原生类型。

        不满足时 _disk_write 的 except Exception 会把 TypeError 静默吞掉：
        内存层照常工作、磁盘目录空着，重启后白重取一遍。落地时真踩到了。
        """
        import json

        payload = cache._encode_fund_flow(_flow(120, is_market=True))
        json.dumps(payload, ensure_ascii=False)   # 不抛就算过

    def test_amounts_keep_their_precision_through_json(self):
        """资金流金额到分位，默认 double_precision=10 会截尾。"""
        import json

        value = {"fund_flow": pd.DataFrame({"主力净流入-净额": [-81303900.0, 37874500.25]}),
                 "is_market": False}
        restored = cache._decode_fund_flow(
            json.loads(json.dumps(cache._encode_fund_flow(value))))
        assert list(restored["fund_flow"]["主力净流入-净额"]) == [-81303900.0, 37874500.25]

    def test_real_akshare_dates_survive(self):
        """AkShare 的 `日期` 是 datetime.date，没有 .item()、json 也不认。

        构造测试用字符串日期照不出这个洞——落地时就是这样：单测全绿、磁盘目录空着。
        回读成字符串是可以的：_date_to_ns 对 str 和 date 走的是等价分支
        （都落到 strptime(str(x)[:10])），转出来的纳秒数完全相同。
        """
        import datetime
        import json

        value = {"fund_flow": pd.DataFrame({
            "日期": [datetime.date(2026, 9, 3), datetime.date(2026, 9, 4)],
            "主力净流入-净额": [1.0, -0.0],
        }), "is_market": False}

        payload = cache._encode_fund_flow(value)
        json.dumps(payload)     # 不抛就算过

        restored = cache._decode_fund_flow(json.loads(json.dumps(payload)))
        assert list(restored["fund_flow"]["日期"]) == ["2026-09-03", "2026-09-04"]

        from finmcp.datasource.cn_stock_source import CNStockDataSource
        source = CNStockDataSource.__new__(CNStockDataSource)
        assert [source._date_to_ns(d) for d in restored["fund_flow"]["日期"]] == \
               [source._date_to_ns(d) for d in value["fund_flow"]["日期"]]


class TestPerNamespaceSwitch:
    """``CACHE_<NS>_ENABLED``：只关掉一层，别的照旧。

    存在的理由是 A/B——要量"这一层缓存值多少"，就得只关它。只有总开关时，
    关掉它连报告缓存一起没了，测出来的是两层加起来的收益，没法归因。
    """

    def test_one_namespace_can_be_switched_off_on_its_own(self, monkeypatch):
        from finmcp import config

        monkeypatch.setattr(config, "CACHE_ENABLED", True)
        monkeypatch.setenv("CACHE_FUND_FLOW_ENABLED", "0")

        assert config.cache_enabled("fund_flow") is False
        assert config.cache_enabled("realtime") is True

    def test_it_falls_back_to_the_global_switch(self, monkeypatch):
        from finmcp import config

        monkeypatch.delenv("CACHE_FUND_FLOW_ENABLED", raising=False)
        monkeypatch.setattr(config, "CACHE_ENABLED", False)
        assert config.cache_enabled("fund_flow") is False
        monkeypatch.setattr(config, "CACHE_ENABLED", True)
        assert config.cache_enabled("fund_flow") is True

    def test_an_explicit_flag_beats_the_config(self, monkeypatch):
        """`Cache(ns, enabled=True)` 不能在 CACHE_ENABLED=0 的环境里静默关闭。

        conftest 就是这么跑的（整层默认关），落地时这一条挂了 26 个测试。
        """
        from finmcp import config

        monkeypatch.setattr(config, "CACHE_ENABLED", False)
        built = cache.Cache(cache.namespace("fund_flow"), enabled=True)
        assert built.enabled is True


class TestPartialFundFlowOnlyBlocksTheToolThatRendersHistory:
    """``fund_flow:partial`` 拦缓存的判据是"行数不够"，而只有 full 渲染历史表。

    2026-09-07 定位：brief 只印"当日主力净流入"一行，1 行和 120 行对它的输出完全
    一样；而它的页面兜底本来就不触发（要求 requirements.fund_flow_page），所以
    partial 对它是**永久状态**，"下次页面可能就成功了"这个理由等不到那个下次。
    代价实测：收盘后 brief 15 次调用 15 次 Report cache skipped、0 次命中，每次
    都全额打上游，而且连续三次调用因为上游当日行抖动给出三个不同的值。
    """

    @staticmethod
    def _partial(*, fund_flow: bool, fund_flow_page: bool, complete: bool) -> bool:
        """照搬 cn_stock_source 里那个判据，参数化后单独测。"""
        from finmcp.datasource.cn_stock_source import (
            _fund_flow_needs_page, _is_fetch_failure,
        )

        value = {"complete": complete}
        return bool(
            fund_flow
            and fund_flow_page
            and not _is_fetch_failure(value)
            and _fund_flow_needs_page(value)
        )

    def test_full_still_refuses_to_cache_a_one_row_history(self):
        """full 渲染历史表，1 行确实是降级，仍然要拦。"""
        assert self._partial(fund_flow=True, fund_flow_page=True, complete=False) is True

    def test_brief_is_cacheable_with_only_the_delay_row(self):
        """brief 不渲染历史表，一行就是它的完整答案。"""
        assert self._partial(fund_flow=True, fund_flow_page=False, complete=False) is False

    def test_a_complete_history_is_never_partial(self):
        for page in (True, False):
            assert self._partial(fund_flow=True, fund_flow_page=page, complete=True) is False

    def test_a_call_that_did_not_ask_for_fund_flow_is_never_partial(self):
        assert self._partial(fund_flow=False, fund_flow_page=True, complete=False) is False

    def test_the_source_still_wires_the_flag_the_same_way(self):
        """判据不能只活在测试里——源码里那四个条件必须还是这四个。"""
        import inspect

        from finmcp.datasource import cn_stock_source

        src = inspect.getsource(cn_stock_source)
        block = src[src.index("fund_flow_partial = ("):]
        block = block[: block.index("\n        )")]
        for needed in ("requirements.fund_flow", "requirements.fund_flow_page",
                       "_is_fetch_failure", "_fund_flow_needs_page"):
            assert needed in block, f"{needed} 不在判据里了：{block}"

    def test_the_landing_guard_is_a_separate_concern(self):
        """"当日那一行还没落地"由 fund_flow_lagging 管，和行数无关，不受这次改动影响。"""
        from finmcp.cache import PHASE_CLOSED, is_cacheable_report

        assert is_cacheable_report("报告正文", phase=PHASE_CLOSED,
                                   fund_flow_lagging=True) is False
        assert is_cacheable_report("报告正文", phase=PHASE_CLOSED,
                                   fund_flow_lagging=False) is True
