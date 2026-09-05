"""平台底座的契约。

盯的是架构承诺（docs/architecture.md）：接一个新平台只用写一个类加
一行注册；四道闸门按便宜到贵的顺序拦；逐级回退和交叉合成用同一个 resolve。
"接新平台"这件事将来会真的发生（同花顺、雪球），所以它得有测试守着。
"""

from __future__ import annotations

import pytest

from finmcp.datasource import platform as pf


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(pf._PLATFORMS)
    pf._PLATFORMS.clear()
    yield
    pf._PLATFORMS.clear()
    pf._PLATFORMS.update(saved)


class _Stub(pf.Platform):
    def __init__(self, name, value=None, *, caps=("thing",), raises=None,
                 supports=True, degraded=False):
        self.name = self.label = name
        self.capabilities = frozenset(caps)
        self._value, self._raises = value, raises
        self._supports, self._degraded = supports, degraded
        self.calls = 0

    def supports(self, capability, request):
        return self._supports

    def degraded(self):
        return self._degraded

    def fetch_thing(self, request):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._value

    # 声明了 other 能力的桩也得有对应方法——注册时会校验，这正是被测的行为之一。
    fetch_other = fetch_thing


class _Breaker:
    def __init__(self, open_=False):
        self.open, self.records = open_, []

    def should_skip(self):
        return self.open

    def record(self, *, success):
        self.records.append(success)


# --- 注册：声明和实现必须对得上 ---------------------------------------------


def test_declaring_a_capability_without_implementing_it_fails_at_registration():
    """拼错能力名不该等到线上少一个源才发现。"""
    class Broken(pf.Platform):
        name = label = "broken"
        capabilities = frozenset({"kline"})      # 没有 fetch_kline

    with pytest.raises(ValueError, match="没有对应的 fetch_ 方法"):
        pf.register(Broken())


def test_a_nameless_platform_is_rejected():
    with pytest.raises(ValueError, match="没有 name"):
        pf.register(_Stub(""))


def test_duplicate_names_are_rejected_unless_replacing():
    pf.register(_Stub("a"))
    with pytest.raises(ValueError, match="重名"):
        pf.register(_Stub("a"))
    pf.register(_Stub("a"), replace=True)


def test_registered_can_be_filtered_by_capability():
    pf.register(_Stub("a", caps=("thing",)))
    pf.register(_Stub("b", caps=("other",)))
    assert set(pf.registered()) == {"a", "b"}
    assert pf.registered("thing") == ("a",)


# --- 配置：顺序按维度，配错只是少一个源 --------------------------------------


def test_the_configured_order_decides_who_wins(monkeypatch):
    pf.register(_Stub("a", "A"))
    pf.register(_Stub("b", "B"))
    monkeypatch.setenv("T_PROVIDERS", "a,b")
    assert pf.resolve("thing", None, order=pf.configured_order("thing", "T_PROVIDERS", ())).value == "A"
    monkeypatch.setenv("T_PROVIDERS", "b,a")
    assert pf.resolve("thing", None, order=pf.configured_order("thing", "T_PROVIDERS", ())).value == "B"


def test_the_whole_tier_can_be_turned_off(monkeypatch):
    pf.register(_Stub("a", "A"))
    monkeypatch.setenv("T_PROVIDERS", "off")
    assert pf.configured_order("thing", "T_PROVIDERS", ("a",)) == ()


def test_an_unknown_name_is_ignored_not_fatal(monkeypatch):
    pf.register(_Stub("a", "A"))
    monkeypatch.setenv("T_PROVIDERS", "nope,a")
    assert pf.configured_order("thing", "T_PROVIDERS", ()) == ("a",)


def test_a_platform_without_the_capability_is_ignored(monkeypatch):
    pf.register(_Stub("a", "A", caps=("other",)))
    monkeypatch.setenv("T_PROVIDERS", "a")
    assert pf.configured_order("thing", "T_PROVIDERS", ()) == ()


# --- 逐级回退 ---------------------------------------------------------------


def test_an_empty_result_falls_through_just_like_a_failure():
    """空结果和抛异常一样要往下走——"没给出结果"不等于"出错了"。"""
    first = _Stub("a", None)
    pf.register(first)
    pf.register(_Stub("b", "B"))
    got = pf.resolve("thing", None, order=("a", "b"))
    assert first.calls == 1 and got.value == "B" and got.platform == "b"


def test_an_empty_dataframe_counts_as_empty():
    """AkShare 窗口内没数据时给的是空表不是 None，而 DataFrame 没有真值语义。"""
    pd = pytest.importorskip("pandas")
    pf.register(_Stub("a", pd.DataFrame()))
    pf.register(_Stub("b", pd.DataFrame([{"x": 1}])))
    assert pf.resolve("thing", None, order=("a", "b")).platform == "b"


def test_an_exception_does_not_stop_the_chain():
    pf.register(_Stub("a", raises=RuntimeError("boom")))
    pf.register(_Stub("b", "B"))
    assert pf.resolve("thing", None, order=("a", "b")).value == "B"


# --- 四道闸门 ---------------------------------------------------------------


def test_a_degraded_platform_is_skipped_without_a_request():
    """已知必败的平台连请求都不该发。"""
    down = _Stub("a", "A", degraded=True)
    pf.register(down)
    pf.register(_Stub("b", "B"))
    status: dict = {}
    got = pf.resolve("thing", None, order=("a", "b"), status=status)
    assert down.calls == 0 and status["a_degraded"] and got.platform == "b"


def test_an_open_breaker_is_skipped_without_a_request():
    down = _Stub("a", "A")
    pf.register(down)
    pf.register(_Stub("b", "B"))
    breakers = {"a": _Breaker(open_=True)}
    status: dict = {}
    got = pf.resolve("thing", None, order=("a", "b"),
                     status=status, breaker_for=breakers.get)
    assert down.calls == 0 and status["a_breaker_open"] and got.platform == "b"


def test_an_unsupported_request_costs_no_request():
    picky = _Stub("a", "A", supports=False)
    pf.register(picky)
    pf.register(_Stub("b", "B"))
    status: dict = {}
    got = pf.resolve("thing", None, order=("a", "b"), status=status)
    assert picky.calls == 0 and status["a_unsupported"] and got.platform == "b"


def test_the_breaker_records_both_outcomes():
    pf.register(_Stub("a", raises=RuntimeError("boom")))
    pf.register(_Stub("b", "B"))
    breakers = {"a": _Breaker(), "b": _Breaker()}
    pf.resolve("thing", None, order=("a", "b"), breaker_for=breakers.get)
    assert breakers["a"].records == [False]
    assert breakers["b"].records == [True]


# --- 覆盖缺口 vs 一次安静的失败 ----------------------------------------------


def test_all_platforms_rejecting_is_a_coverage_gap():
    pf.register(_Stub("a", raises=KeyError("no such code")))
    pf.register(_Stub("b", supports=False))
    status: dict = {}
    assert pf.resolve("thing", None, order=("a", "b"), status=status) is None
    assert status["unsupported"] is True


def test_a_plain_failure_is_not_a_coverage_gap():
    pf.register(_Stub("a", raises=RuntimeError("timeout")))
    status: dict = {}
    assert pf.resolve("thing", None, order=("a",), status=status) is None
    assert status.get("unsupported") is None


# --- 交叉合成 ---------------------------------------------------------------


def test_merge_combines_partial_results_from_several_platforms():
    """A 给了 a、B 给了 b，合起来才完整——不能因为 A 先返回就丢掉 b。"""
    pf.register(_Stub("a", {"name": "贵州茅台"}))
    pf.register(_Stub("b", {"cap": 16626}))

    def merge(base, extra):
        return {**extra, **(base or {})}      # 已有值的不被后来的覆盖

    got = pf.resolve("thing", None, order=("a", "b"), merge=merge,
                     enough=lambda v: "cap" in v)
    assert got.value == {"name": "贵州茅台", "cap": 16626}
    assert got.merged_from == ("a", "b") and got.source == "a+b"


def test_enough_stops_the_chain_early():
    second = _Stub("b", {"cap": 1})
    pf.register(_Stub("a", {"name": "x", "cap": 0}))
    pf.register(second)
    got = pf.resolve("thing", None, order=("a", "b"),
                     merge=lambda base, extra: {**extra, **(base or {})},
                     enough=lambda v: "cap" in v)
    assert second.calls == 0 and got.source == "a"


def test_a_partial_result_is_returned_even_if_never_enough():
    """走完全部平台仍不"够"，半个结果也比没有强。"""
    pf.register(_Stub("a", {"name": "x"}))
    got = pf.resolve("thing", None, order=("a",),
                     merge=lambda base, extra: {**extra, **(base or {})},
                     enough=lambda v: "cap" in v)
    assert got is not None and got.value == {"name": "x"}


def test_first_wins_when_no_merge_is_given():
    second = _Stub("b", "B")
    pf.register(_Stub("a", "A"))
    pf.register(second)
    assert pf.resolve("thing", None, order=("a", "b")).value == "A"
    assert second.calls == 0


# --- 能力契约：谁提供这个能力，归一后都得是同一个结构 -------------------------


def test_a_provider_that_ignores_the_contract_is_skipped_not_trusted():
    """写坏的新 provider 应该降级到旧的，而不是把脏数据灌进报告。"""
    class Good:
        pass

    pf.define_capability("thing", Good, describe="Good")
    try:
        wrong = _Stub("wrong", "我不是 Good")     # 归一没做，直接返回原始串
        pf.register(wrong)
        pf.register(_Stub("right", Good()))
        status: dict = {}
        got = pf.resolve("thing", None, order=("wrong", "right"), status=status)
        assert wrong.calls == 1                    # 请求发了，是返回值被判不合格
        assert "wrong_contract_violation" in status
        assert got.platform == "right" and isinstance(got.value, Good)
    finally:
        pf._CONTRACTS.pop("thing", None)


def test_a_contract_can_be_a_validator_not_just_a_type():
    """K 线返回 DataFrame，光判类型说明不了列对不对——而列不对正是接新源最常见的错。"""
    pf.define_capability("thing", lambda v: isinstance(v, dict) and "成交额" in v,
                         describe="含成交额的表")
    try:
        pf.register(_Stub("missing_col", {"成交量": 1}))
        pf.register(_Stub("complete", {"成交量": 1, "成交额": 2}))
        got = pf.resolve("thing", None, order=("missing_col", "complete"))
        assert got.platform == "complete"
    finally:
        pf._CONTRACTS.pop("thing", None)


def test_a_capability_without_a_registered_contract_is_not_validated():
    """没登记契约就不校验——迁移期间新旧能力可以共存。"""
    pf.register(_Stub("a", "随便什么"))
    assert pf.resolve("thing", None, order=("a",)).value == "随便什么"


def test_a_throwing_validator_counts_as_a_violation():
    """校验函数自己写错了，也不能把脏数据放过去。"""
    pf.define_capability("thing", lambda v: v.this_attribute_does_not_exist)
    try:
        pf.register(_Stub("a", "x"))
        status: dict = {}
        assert pf.resolve("thing", None, order=("a",), status=status) is None
        assert "校验函数自己抛了" in status["a_contract_violation"]
    finally:
        pf._CONTRACTS.pop("thing", None)
