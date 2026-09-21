"""数据源层的 `fund_flow` / `realtime` 缓存（docs/cache-design.md §4.7）。

收编的理由是跨工具重复取数：`brief` / `medium` / `full` 是三个不同的**报告**缓存键，
底下却用同一份资金流和同一份基本数据。2026-09-06 实测 22 次调用里，
资金流取了 56 次只涉及 23 个标的（2.4×），基本数据 68 次 / 24 个标的（2.8×），
其中 44 次资金流走了浏览器兜底、只涉及 22 个标的——一半的页面加载是重复的，
每次 P50 6.4s。

这里最要紧的是**行数守卫**：页面兜底只给 120 行，主源给全部历史。缓存了少的那份
之后不能让要得多的请求命中它，否则缓存把数据变少了（AGENTS §一）。
"""

import numpy as np
import pandas as pd
import pytest
from typing import Optional

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
    """``fund_flow:partial`` 与报告里那句"只取到 N/M"必须是**同一个判定**。

    两层各自数一遍就会数出两个答案，而且两个方向都错得起来：拿 ``limit`` 当基准的那侧
    把"新股只有 20 个交易日、页面给满 20 行"当成缺 40 天而拦住缓存（永久回源）；拿 K 线
    交易日数当基准的那侧又放过"源自称全份却只给 3 行"（README 的承诺就此不成立）。
    所以下面每格都同时问渲染层和缓存层。

    工具范围这条不变：只有渲染历史表的请求参与（``fund_flow_page``，即 full 与钉日期的
    查询）。brief 只印"当日主力净流入"一行，1 行和 120 行对它的输出完全一样，partial 对它
    是**永久状态**——2026-09-07 实测收盘后 brief 15 次调用 15 次 Report cache skipped、
    0 次命中，每次都全额打上游，还因为上游当日行抖动给出三个不同的值。
    """

    _LIMIT = 60

    @staticmethod
    def _ns_days(n: int, last: str = "2026-09-18"):
        """造 n 个连续日期的 ns 值，**用生产同一套换算**（本地时区的 ``date_to_ns``）。

        换成 ``pd.DatetimeIndex.astype('int64')`` 是 UTC 口径，非 UTC 机器上会和
        ``_date_to_ns`` 差一个时区偏移，测试就成了自证。
        """
        from finmcp.datasource import fund_flow_source as ffs

        end = pd.Timestamp(last)
        return [ffs.date_to_ns((end - pd.Timedelta(days=i)).strftime("%Y-%m-%d"))
                for i in range(n)][::-1]

    @classmethod
    def _verdict(cls, *, flow_rows: int, kline_rows, provider: str = "eastmoney",
                 complete=None, pinned=None, limit: Optional[int] = None,
                 history_table: bool = True):
        """同一次请求，问两个层：报告印不印那句、缓存拦不拦。

        ``history_table=False`` 是 brief / medium 的形状：不画历史表，但钉日期时照样
        开着 ``fund_flow_page``，所以它也要过一遍这段判据。
        """
        import io

        from finmcp import research
        from finmcp.datasource import fund_flow_source as ffs
        from finmcp.datasource.cn_stock_source import _fund_flow_report_incomplete

        limit = cls._LIMIT if limit is None else limit
        flow_days = cls._ns_days(flow_rows, last=pinned or "2026-09-18")
        data = {"_DS_FUND_FLOW": {"DATE": np.array(flow_days, dtype=np.int64)}}
        if kline_rows is not None:
            data["DATE"] = np.array(cls._ns_days(kline_rows, last=pinned or "2026-09-18"),
                                    dtype=np.int64)
        if pinned:
            data["QUERY_DATE"] = pinned
        printed = ""
        if history_table:   # 只有 full 会画这张表，见 mcp_app 的 include_historical_fund_flow
            fp = io.StringIO()
            research.build_historical_fund_flow_data(fp, data, limit)
            printed = fp.getvalue()

        value = {"fund_flow": pd.DataFrame({"日期": pd.to_datetime(flow_days, unit="ns")
                                            .strftime("%Y-%m-%d")}),
                 "is_market": False, "provider": provider}
        if complete is not None:
            value["complete"] = complete
        supply = ffs.fund_flow_supply(
            data["_DS_FUND_FLOW"]["DATE"], data.get("DATE"),
            limit if history_table else 0, ffs.date_to_ns(pinned))
        need = ffs.FundFlowNeed(history_rows=limit if history_table else 0,
                                pinned_date=pinned)
        return "只取到" in printed, _fund_flow_report_incomplete(value, need, supply)

    #: (说明, 给到的行数, K 线交易日数, 源, complete)
    SHAPES = [
        ("主源自称全份却只给 3 行，K 线有 486 个交易日", 3, 486, "eastmoney", True),
        ("新股：K 线只有 20 天，页面给满 20 行", 20, 20, "page_fallback", None),
        ("页面只给到 3 行，K 线有 486 个交易日", 3, 486, "page_fallback", None),
        ("页面给满 120 行，请求 60 行", 120, 486, "page_fallback", None),
        ("delay 那单行（源自己说不全）", 1, 486, "eastmoney_delay", False),
        ("新股全份 3 行，K 线也只有 3 天", 3, 3, "eastmoney", True),
    ]

    @pytest.mark.parametrize("label,flow_rows,kline_rows,provider,complete", SHAPES)
    def test_the_report_and_the_cache_gate_never_disagree(
            self, label, flow_rows, kline_rows, provider, complete):
        note, blocked = self._verdict(flow_rows=flow_rows, kline_rows=kline_rows,
                                      provider=provider, complete=complete)
        assert note == blocked, f"{label}: 报告印={note} 缓存拦={blocked}，不同进同出"

    def test_only_the_history_rendering_tools_are_gated(self):
        """外层那两道闸：不渲染历史表（brief/medium）就永不拦。

        ``_verdict`` 自己复算了这段接线（``limit if history_table else 0``），所以上面
        那几题证明的是**判据本身**对，证明不了源码还照这样接。这题补的就是接线：
        少了 ``fund_flow_history_table`` 那一支，钉日期的 brief 会按请求行数被拦成
        永久回源，而上面每一题照样全绿。
        """
        import inspect

        from finmcp.datasource import cn_stock_source

        src = inspect.getsource(cn_stock_source)
        block = src[src.index("supply = fund_flow_source.fund_flow_supply("):]
        marker = 'fetch_failures.append("fund_flow:partial")'
        block = block[: block.index(marker) + len(marker)]
        # 外层条件必须是"渲染历史表或钉日期"——曾经用 fund_flow_page，它改成配置链
        # 推导之后对 brief/medium 恒为真，规则 2 会把 delay 供数的 brief 拦成永久回源。
        for needed in ("requirements.fund_flow_history_table",
                       "requirements.fund_flow_pinned_date",
                       "_fund_flow_report_incomplete",
                       "requirements.fund_flow_rows", marker):
            assert needed in block, f"{needed} 不在那一处里了：{block}"
        assert "requirements.fund_flow_page and _fund_flow_report_incomplete" not in block

    def test_the_renderer_is_handed_the_same_row_count_the_gate_uses(self):
        """渲染层的 limit 必须是 ``requirements.fund_flow_rows``，不是工具的原始参数。

        缓存那一侧读的就是 ``requirements.fund_flow_rows``；渲染层若改读原始参数，
        两层立刻数出两个数——``fund_flow_limit`` 没有任何上下限校验，客户端传 0 时
        ``requirements`` 归一成 15 而原始值是 0，报告整段不打印，缓存却按 15 判"给全了"。
        下面第二段断言证明这不是空断言：那两个数真的会不一样。
        """
        import ast
        from pathlib import Path

        # 读文件而不是 inspect：``from finmcp import mcp_app`` 拿到的是那个
        # ``QtfMCP`` 实例，不是模块。
        source = (Path(__file__).resolve().parents[1] / "finmcp" / "mcp_app.py").read_text(
            encoding="utf-8"
        )
        passed = [
            kw.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "historical_fund_flow_limit"
        ]
        assert passed, "mcp_app 不再往渲染层传 historical_fund_flow_limit 了？"
        for value in passed:
            assert (isinstance(value, ast.Attribute)
                    and value.attr == "fund_flow_rows"
                    and getattr(value.value, "id", "") == "requirements"), (
                "渲染层的行数必须取自 requirements.fund_flow_rows，"
                f"现在传的是 {ast.dump(value)}"
            )

        # 这一题测得出问题吗：两个数确实不是一个数——``requirements`` 收的是归一过的
        # 表达式而不是裸参数，所以"改传原始参数"是真的会改变行为，不是同义替换。
        built = [
            kw.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "FetchRequirements"
            for kw in node.keywords
            if kw.arg == "fund_flow_rows"
        ]
        assert built and not any(
            isinstance(v, ast.Name) and v.id == "fund_flow_limit" for v in built
        ), "fund_flow_rows 现在就是裸的 fund_flow_limit，这题于是什么也没守住"

    def test_a_page_missing_the_pinned_date_is_not_cacheable(self):
        """钉的那一天不在帧里：行数照样凑得满，但那天根本没有——拦。"""
        note, blocked = self._verdict(flow_rows=120, kline_rows=486,
                                      provider="page_fallback", pinned="2019-01-01")
        assert blocked is True and note is False

    def test_a_brief_never_pays_for_a_table_it_does_not_draw(self):
        """同一份数据：full 拦（它要印那句），钉日期的 brief 不拦（它一张表都不画）。

        这一格是这次收口最容易踩的坑——brief 钉日期也带着 ``fund_flow_page``，如果按
        请求行数拦，它就成了**永久状态**：每次都全额打上游，而它的输出一字不变。
        """
        same = dict(flow_rows=3, kline_rows=486, complete=True, pinned="2026-09-18")
        assert self._verdict(**same) == (True, True)
        assert self._verdict(history_table=False, **same) == (False, False)

    def test_a_missing_kline_baseline_blocks_without_nagging_the_reader(self):
        """K 线那一维没给日期时无从判断"该有几行"，报告不印，但缓存保守拦。

        方向和不一致是故意的：宁可少命中一次缓存，也不能把缺一段的报告冻整个纪元。
        反过来（印了句子却照常进缓存）才是 README 兜不住的那种。
        """
        note, blocked = self._verdict(flow_rows=3, kline_rows=None,
                                      provider="page_fallback")
        assert note is False and blocked is True

    def test_a_call_that_did_not_ask_for_fund_flow_is_never_partial(self):
        from finmcp.datasource.cn_stock_source import _is_fetch_failure

        assert _is_fetch_failure({"fund_flow": None}) is False

    def test_the_landing_guard_is_a_separate_concern(self):
        """"当日那一行还没落地"由 fund_flow_lagging 管，和行数无关，不受这次改动影响。"""
        from finmcp.cache import PHASE_CLOSED, is_cacheable_report

        assert is_cacheable_report("报告正文", phase=PHASE_CLOSED,
                                   fund_flow_lagging=True) is False
        assert is_cacheable_report("报告正文", phase=PHASE_CLOSED,
                                   fund_flow_lagging=False) is True


class TestWriteBackShrink:
    """只增不减的确切边界（实测钉住，别被下次"顺手改松"吃掉）。

    它挡的是"缓存丢一天"，代价是上游把行数改小的自我修正会**整份**被拒（不是部分
    合并），要等纪元翻转。行数相同的修正照常落地——所以东财那种"同一天两个值来回
    翻"的抖动不会被冻在这里。
    """

    def _flow(self, rows: int, amount: float) -> dict:
        """整帧同一个常量，断言才能一眼看出新值有没有以任何形式混进来。"""
        return {
            "fund_flow": pd.DataFrame({
                "日期": pd.date_range("2026-01-01", periods=rows).astype(str),
                "主力净流入-净额": [amount] * rows,
            }),
            "provider": "page_fallback",
        }

    def test_a_smaller_frame_is_rejected_whole(self):
        css.store_fund_flow("SH600519", self._flow(121, 1.0))
        css.store_fund_flow("SH600519", self._flow(120, 9.0))
        # Cache.get 给的是**值**本身，不是 entry
        kept = cache.cache_for(cache.FUND_FLOW_NAMESPACE.name).get(
            cache.key_for(cache.FUND_FLOW_NAMESPACE.name, "SH600519"))
        assert css._fund_flow_rows(kept) == 121
        # 整份拒绝：新值没以任何形式混进来
        assert kept["fund_flow"]["主力净流入-净额"].iloc[-1] == 1.0

    def test_an_equal_length_correction_still_lands(self):
        """上游改了值但没少行——这种修正必须能进来，否则就把"变准"挡住了。"""
        css.store_fund_flow("SH600519", self._flow(121, 1.0))
        css.store_fund_flow("SH600519", self._flow(121, 7.0))
        kept = cache.cache_for(cache.FUND_FLOW_NAMESPACE.name).get(
            cache.key_for(cache.FUND_FLOW_NAMESPACE.name, "SH600519"))
        assert kept["fund_flow"]["主力净流入-净额"].iloc[-1] == 7.0
