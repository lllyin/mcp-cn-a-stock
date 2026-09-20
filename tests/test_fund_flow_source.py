"""个股 / 指数资金流在平台层上的接线。

盯四件事：契约、请求怎么归一成东财认的写法、两级顺序（完整历史优先、不完整只留
行数多的）、push2delay 那一行和 AkShare 的表逐字相同。注册表和四道闸门归 test_platform.py。
"""

from __future__ import annotations

import datetime
import types

import pandas as pd
import pytest

from finmcp import cache
from finmcp.datasource import cn_stock_source as source_module
from finmcp.datasource import fund_flow_source as ffs
from finmcp.datasource import platform as pf
from finmcp.datasource.platforms import eastmoney

COLUMNS = list(ffs.FUND_FLOW_COLUMNS)

# push2delay 2026-09-06 对 0.399006 的原样返回
DELAY_KLINE = (
    "2026-09-04,-7200599040.0,8721076224.0,-1520476160.0,-3684253696.0,-3516345344.0,"
    "-1.43,1.73,-0.30,-0.73,-0.70,3286.55,-0.78,0.00,0.00"
)


def _frame(rows: int = 3) -> pd.DataFrame:
    if rows == 0:
        return pd.DataFrame(columns=COLUMNS)
    start = datetime.date(2026, 1, 1)
    return pd.DataFrame([
        {**{c: float(i) for c in COLUMNS[1:]},
         "日期": start + datetime.timedelta(days=i)}
        for i in range(rows)
    ])[COLUMNS]


# --- 契约 -------------------------------------------------------------------


def test_the_contract_is_registered():
    assert pf.contract_of(ffs.CAPABILITY) is ffs._honours_contract


def test_a_full_table_honours_the_contract():
    assert ffs._honours_contract(ffs.FundFlowHistory(_frame())) is True


def test_a_missing_column_or_empty_table_violates_it():
    assert ffs._honours_contract(ffs.FundFlowHistory(_frame().drop(columns=["小单净流入-净占比"]))) is False
    assert ffs._honours_contract(ffs.FundFlowHistory(_frame(0))) is False
    assert ffs._honours_contract(_frame()) is False          # 裸表不行，要带 complete
    assert ffs._honours_contract({"日期": []}) is False


# --- 请求：本项目的写法 → 东财认的写法 -----------------------------------------


@pytest.mark.parametrize("code,symbol,is_index,exchange,secid", [
    ("600519", "SH600519", False, "sh", "1.600519"),
    ("000333", "SZ000333", False, "sz", "0.000333"),
    ("920021", "BJ920021", False, "sz", "0.920021"),    # AkShare 里 bj 和 sz 同为市场号 0
    ("000688", "SH000688", True, "sh", "1.000688"),
    ("399006", "SZ399006", True, "sz", "0.399006"),
    ("159326", None, False, "sz", "0.159326"),
    ("688008", "SH688008", False, "sh", "1.688008"),    # 科创板也是 6 开头
    ("899050", "BJ899050", True, "sz", "0.899050"),     # 北交所指数
])
def test_exchange_and_secid(code, symbol, is_index, exchange, secid):
    request = ffs.FundFlowRequest(code=code, symbol=symbol, is_index=is_index)
    assert (request.exchange, request.secid) == (exchange, secid)


@pytest.mark.parametrize("code,symbol", [
    ("512480", "SH512480"),     # 半导体ETF国联安
    ("588200", "SH588200"),     # 科创芯片ETF嘉实
    ("510300", "SH510300"),     # 沪深300ETF
    ("511990", "SH511990"),     # 华宝添益（货币ETF）
])
def test_shanghai_funds_are_not_mistaken_for_shenzhen(code, symbol):
    """沪市 5 开头的基金必须判成沪市。

    原先这里写 ``code.startswith("6")``，于是 ``SH512480`` 算出 ``0.512480``，东财
    对这个 secid 返回 ``rc=100``、0 行，而 ``1.512480`` 有 121 行。线上后果不是报错
    而是**静默取空**：2026-09-08 伪装通道一冷却，``eastmoney`` 被跳过、
    ``eastmoney_delay`` 共用这个错 secid 也拿不到，沪市 ETF 的资金流向整维变成
    "暂无资金流向数据"，当天落地 4 次。
    """
    request = ffs.FundFlowRequest(code=code, symbol=symbol)
    assert (request.exchange, request.secid) == ("sh", f"1.{code}")


@pytest.mark.parametrize("code,exchange", [
    ("512480", "sh"), ("588200", "sh"), ("600519", "sh"), ("688008", "sh"),
    ("159326", "sz"), ("000333", "sz"), ("300223", "sz"),
    ("920021", "sz"),   # 北交所 92 开头：加 "9" 进沪市前缀表就会把它判错
    ("899050", "sz"),
])
def test_code_only_fallback(code, exchange):
    """没有带前缀的 symbol 时按代码猜，这条路 cn_stock_source 会走到。

    ``symbol`` 为空时 ``_fetch_fund_flow_sync`` 照样构造请求（那里有
    ``if symbol else ""``），所以兜底规则也得对，不能只修 symbol 那一支。
    """
    assert ffs.FundFlowRequest(code=code).exchange == exchange


# --- 顺序：完整历史优先，不完整只留行数多的 ---------------------------------


@pytest.fixture
def registry():
    saved = dict(pf._PLATFORMS)
    pf._PLATFORMS.clear()
    yield pf._PLATFORMS
    pf._PLATFORMS.clear()
    pf._PLATFORMS.update(saved)


class _Stub(pf.Platform):
    def __init__(self, name, value=None, raises=None):
        self.name = self.label = name
        self.capabilities = frozenset({ffs.CAPABILITY})
        self._value, self._raises, self.calls = value, raises, 0

    def fetch_fund_flow(self, request):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._value


REQUEST = ffs.FundFlowRequest(code="000688", symbol="SH000688", is_index=True)


def test_a_complete_history_stops_the_chain(registry):
    full = _Stub("a", ffs.FundFlowHistory(_frame(5), complete=True))
    later = _Stub("b", ffs.FundFlowHistory(_frame(1), complete=False))
    pf.register(full); pf.register(later)
    result = ffs.resolve(REQUEST, order=("a", "b"))
    assert result.provider == "a" and result.complete is True and len(result.frame) == 5
    assert later.calls == 0        # 新股只有 5 行也是"全部历史"，不该再问下一个


def test_a_failed_primary_falls_through_to_the_partial_backup(registry):
    pf.register(_Stub("a", raises=ConnectionError("push2his refused")))
    pf.register(_Stub("b", ffs.FundFlowHistory(_frame(1), complete=False)))
    result = ffs.resolve(REQUEST, order=("a", "b"))
    assert result.provider == "b" and result.complete is False and len(result.frame) == 1


def test_partial_results_keep_the_longer_one(registry):
    pf.register(_Stub("a", ffs.FundFlowHistory(_frame(1), complete=False)))
    pf.register(_Stub("b", ffs.FundFlowHistory(_frame(3), complete=False)))
    pf.register(_Stub("c", ffs.FundFlowHistory(_frame(2), complete=False)))
    result = ffs.resolve(REQUEST, order=("a", "b", "c"))
    assert len(result.frame) == 3 and result.complete is False
    assert result.provider == "a+b+c"   # 合成时记全，日志里看得出是拼的


def test_nothing_when_every_source_fails(registry):
    pf.register(_Stub("a", raises=ConnectionError("x")))
    pf.register(_Stub("b", None))
    assert ffs.resolve(REQUEST, order=("a", "b")) is None


def test_default_order_and_env_switch(monkeypatch):
    assert ffs.DEFAULT_PROVIDER_ORDER == ("eastmoney", "eastmoney_delay")
    monkeypatch.setenv(ffs.PROVIDER_ORDER_ENV, "eastmoney")
    assert ffs.configured_order() == ("eastmoney",)
    monkeypatch.setenv(ffs.PROVIDER_ORDER_ENV, "off")
    assert ffs.configured_order() == ()


# --- push2delay：和 AkShare 的表逐字相同 -----------------------------------------


def _akshare_reference(klines: list) -> pd.DataFrame:
    """``ak.stock_individual_fund_flow`` 拿到 klines 之后做的事，逐行照抄。"""
    temp_df = pd.DataFrame([item.split(",") for item in klines])
    temp_df.columns = [
        "日期", "主力净流入-净额", "小单净流入-净额", "中单净流入-净额", "大单净流入-净额",
        "超大单净流入-净额", "主力净流入-净占比", "小单净流入-净占比", "中单净流入-净占比",
        "大单净流入-净占比", "超大单净流入-净占比", "收盘价", "涨跌幅", "-", "-",
    ]
    temp_df = temp_df[COLUMNS]
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    for column in COLUMNS[1:]:
        temp_df[column] = pd.to_numeric(temp_df[column], errors="coerce")
    return temp_df


def test_delay_frame_matches_akshare_step_for_step():
    ours = eastmoney._fund_flow_frame([DELAY_KLINE])
    pd.testing.assert_frame_equal(ours, _akshare_reference([DELAY_KLINE]), check_dtype=True, check_exact=True)
    assert ours["日期"].iloc[0] == datetime.date(2026, 9, 4)
    assert ours["主力净流入-净额"].iloc[0] == -7200599040.0
    assert ours["收盘价"].iloc[0] == 3286.55


def test_delay_platform_returns_a_partial_history(monkeypatch):
    platform = pf.get("eastmoney_delay")
    seen = {}

    def fake_get(secid):
        seen["secid"] = secid
        return {"data": {"code": "000688", "klines": [DELAY_KLINE]}}

    monkeypatch.setattr(type(platform), "_get", staticmethod(fake_get))
    history = platform.fetch_fund_flow(REQUEST)
    assert seen == {"secid": "1.000688"}
    assert history.complete is False and history.rows == 1
    assert ffs._honours_contract(history) is True


def test_delay_platform_gives_nothing_for_an_empty_payload(monkeypatch):
    platform = pf.get("eastmoney_delay")
    monkeypatch.setattr(type(platform), "_get", staticmethod(lambda secid: {"data": None}))
    assert platform.fetch_fund_flow(REQUEST) is None


def test_delay_platform_is_not_tied_to_the_impersonation_channel():
    """push2delay 不在伪装通道的接管名单里，通道冷却与它无关。"""
    assert pf.get("eastmoney_delay").degraded() is False
    assert "fund_flow" in pf.get("eastmoney").capabilities


def test_eastmoney_platform_calls_akshare_the_same_way_as_before(monkeypatch):
    import akshare as ak

    seen = {}

    def fake(stock, market):
        seen.update(stock=stock, market=market)
        return _frame(4)

    monkeypatch.setattr(ak, "stock_individual_fund_flow", fake)
    history = pf.get("eastmoney").fetch_fund_flow(REQUEST)
    assert seen == {"stock": "000688", "market": "sh"}
    assert history.complete is True and history.rows == 4


# --- 编排：这一帧覆盖本次需求了吗（页面兜底和报告缓存共用） ---------------------


def test_the_coverage_predicate_decides_fallback_and_caching():
    """一个谓词管两件事：还要不要问下一个源，以及这份报告能不能进跨请求缓存。

    以前是**两个**谓词，而页面返回不带 ``complete`` 键——第二个就把 3 行当全份，
    于是短供报告能进缓存冻满一个 CLOSED 纪元（最长 64 小时）。同一个事实在两处
    给出相反结论，是那次修复的根因。
    """
    need = ffs.FundFlowNeed(history_rows=5)
    satisfies = source_module._fund_flow_satisfies
    assert satisfies(source_module._fetch_failure("fund_flow"), need) is False
    assert satisfies({"fund_flow": _frame(1), "is_market": False, "complete": False}, need) is False
    assert satisfies({"fund_flow": _frame(5), "is_market": False, "complete": True}, need) is True
    # 页面封顶 120 行、不带 complete：够需求才算满足，多一行都不放行
    assert satisfies({"fund_flow": _frame(6), "is_market": False, "provider": "page_fallback"}, need) is True
    assert satisfies({"fund_flow": _frame(4), "is_market": False, "provider": "page_fallback"}, need) is False
    assert satisfies(None, need) is True   # 这次没要资金流，不是失败


def test_fetch_fund_flow_sync_reports_the_partial_backup(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", lambda **k: (_ for _ in ()).throw(ConnectionError("refused")))
    delay = pf.get("eastmoney_delay")
    monkeypatch.setattr(type(delay), "_get", staticmethod(lambda secid: {"data": {"klines": [DELAY_KLINE]}}))

    result = source_module.CNStockDataSource()._fetch_fund_flow_sync("000688", "SH000688")
    assert result["complete"] is False and result["is_market"] is False
    assert len(result["fund_flow"]) == 1
    assert source_module._fund_flow_satisfies(result, ffs.FundFlowNeed(history_rows=5)) is False


def test_fetch_fund_flow_sync_marks_the_primary_complete(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", lambda **k: _frame(6))
    result = source_module.CNStockDataSource()._fetch_fund_flow_sync("600519", "SH600519")
    assert result["complete"] is True and len(result["fund_flow"]) == 6


def test_fetch_fund_flow_sync_fails_when_every_source_is_dry(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", lambda **k: pd.DataFrame())
    delay = pf.get("eastmoney_delay")
    monkeypatch.setattr(type(delay), "_get", staticmethod(lambda secid: {"data": {"klines": []}}))
    result = source_module.CNStockDataSource()._fetch_fund_flow_sync("000688", "SH000688")
    assert source_module._is_fetch_failure(result)


# --- 缓存编解码要把 complete 带过去 -------------------------------------------


def test_cache_codec_round_trips_the_complete_flag():
    value = {"fund_flow": _frame(1), "is_market": False, "complete": False}
    decoded = cache._decode_fund_flow(cache._encode_fund_flow(value))
    assert decoded["complete"] is False and len(decoded["fund_flow"]) == 1
    # 老条目没有这个键：按全份读
    assert cache._decode_fund_flow({"rows": [], "is_market": False})["complete"] is True


def test_cache_codec_round_trips_the_provider():
    value = {"fund_flow": _frame(1), "is_market": False, "complete": True,
             "provider": "eastmoney"}
    decoded = cache._decode_fund_flow(cache._encode_fund_flow(value))
    assert decoded["provider"] == "eastmoney"
    # 老条目没有这个键：按 None 读，告警落"未知来源"，检测本身不受影响
    assert cache._decode_fund_flow({"rows": []})["provider"] is None


def test_provider_endpoint_names_the_upstream():
    # 告警的定位能力：看到 provider 名就知道是哪个接口给的
    assert "push2his" in ffs.provider_endpoint("eastmoney")
    assert "push2delay" in ffs.provider_endpoint("eastmoney_delay")
    assert "zjlx" in ffs.provider_endpoint("page_fallback")
    # 未登记的名字原样返回，告警不能哑掉
    assert ffs.provider_endpoint("someone_new") == "未知来源 someone_new"
    assert ffs.provider_endpoint(None).startswith("未知来源")


# --- 一致性检测：恒等式违反 = 这行不是真实成交的账 ---------------------------
#
# 起因是 2026-09-11：push2his 按请求身份给扰动副本——200、行数齐全、收盘价和
# 涨跌幅是真值，只有各单净额偏 30%-50%。任何可用性指标都看不见，只能靠算术。
# 真值取自服务器 curl 的原样返回（基线与东财页面一致），扰动值取自当时基线
# 比对抓到的那两行。


def _row(date, major, xl, big, mid, small, main_r=0.0, mid_r=0.0, small_r=0.0):
    """构造一行 AkShare 列序的资金流。金额单位元，占比是百分数。"""
    return {
        "日期": date, "收盘价": 17.50, "涨跌幅": -0.51,
        "主力净流入-净额": major, "主力净流入-净占比": main_r,
        "超大单净流入-净额": xl, "超大单净流入-净占比": 0.0,
        "大单净流入-净额": big, "大单净流入-净占比": 0.0,
        "中单净流入-净额": mid, "中单净流入-净占比": mid_r,
        "小单净流入-净额": small, "小单净流入-净占比": small_r,
    }


def test_truth_rows_pass_with_zero_violations():
    # 2026-07-01 / 2026-06-29 的真值（服务器 curl 实测，页面同值）
    frame = pd.DataFrame([
        _row(datetime.date(2026, 7, 1), 54376549.0, 36593445.0, 17783104.0,
             5592288.0, -59968832.0, 3.64, 0.37, -4.01),
        _row(datetime.date(2026, 6, 29), 102946830.0, 33707038.0, 69239792.0,
             -62567200.0, -40379632.0, 6.92, -4.20, -2.71),
    ])
    assert ffs.consistency_violations(frame) == []


def test_page_roundtrip_tolerances_hold():
    # 页面兜底两位小数（万）往返的舍入误差在千元级，容差 1 万元必须放行
    frame = pd.DataFrame([
        _row(datetime.date(2026, 7, 1), 54376500.0, 36593400.0, 17783100.0,
             5592300.0, -59968800.0),
    ])
    assert ffs.consistency_violations(frame) == []


def test_decoy_row_is_caught():
    # 2026-09-11 基线比对抓到的扰动值：主力 +3.3%、超大 +30.5%、大单 -52.6%。
    # 这份副本恰好守住了 主力==超大+大（扰动把大单调成了主力-超大），但四档
    # 之和暴露它：真值小单没跟着动，而原始接口的账恒等于 0。
    frame = pd.DataFrame([
        _row(datetime.date(2026, 7, 1), 56175000.0, 47753100.0, 8421900.0,
             3869700.0, -59968832.0),
    ])
    issues = ffs.consistency_violations(frame)
    assert len(issues) == 1 and "2026-07-01" in issues[0]
    assert "四档之和" in issues[0]


def test_inconsistent_ratio_is_caught_when_amounts_are_self_consistent():
    # 扰动若把金额也调自洽，占比这套必须咬住（互为备份）
    frame = pd.DataFrame([
        _row(datetime.date(2026, 7, 1), 50000000.0, 30000000.0, 20000000.0,
             5000000.0, -55000000.0, 4.00, 0.50, -3.00),
    ])
    issues = ffs.consistency_violations(frame)
    assert len(issues) == 1 and "占比之和" in issues[0]


def test_placeholder_rows_are_skipped():
    # 停牌/占位符是 None：None 与 0 的区分必须保持原样，不能当成 0 去算账
    frame = pd.DataFrame([
        _row(datetime.date(2026, 7, 1), None, None, None, None, None),
    ])
    assert ffs.consistency_violations(frame) == []


def test_empty_or_odd_frames_pass_silently():
    assert ffs.consistency_violations(None) == []
    assert ffs.consistency_violations(pd.DataFrame(columns=COLUMNS)) == []
    # 缺列的表（比如指数那套带交易所前缀的列名）不做金额检查，别误报
    frame = pd.DataFrame([{"日期": datetime.date(2026, 7, 1),
                           "上证-收盘价": 3875.16, "上证-涨跌幅": 0.12}])
    assert ffs.consistency_violations(frame) == []


def test_anomalies_flow_to_raw_data_and_block_cache(monkeypatch):
    # 咽喉点接线：检测命中 → fund_flow_anomalies 进 StockData → raw_data
    # → warnings 与缓存守卫
    from finmcp.datasource.base import FUND_FLOW_ANOMALIES_KEY, StockData

    stock_data = StockData(symbol="SH600489")
    anomalies = ["2026-07-01: 主力(1) != 超大+大(2)，差 -1 元"]
    stock_data.fund_flow_anomalies = anomalies
    raw = stock_data.to_dict()
    assert raw[FUND_FLOW_ANOMALIES_KEY] == anomalies


def test_page_yi_scale_rounding_is_not_a_violation():
    """页面兜底把大额行渲染成两位小数的"亿"（步进 100 万元），四桶各有
    ±50 万的舍入——这是显示粒度，不是扰动。2026-09-13 曾整批误报。"""
    frame = pd.DataFrame([
        # 页面上是 主力-2.17亿 超大-1.63亿 大-0.54亿 中+0.24亿 小+1.93亿：
        # 解析后各带 ±0.005 亿 的舍入，四桶和偏 -100 万仍在容差内
        _row(datetime.date(2026, 3, 23), -217000000.0, -163000000.0,
             -54000000.0, 24000000.0, 193000000.0, -7.91, 0.89, 7.02),
    ])
    assert ffs.consistency_violations(frame, rendered=True) == []
    # 页面行的小单改成 +192,900,000（四桶和 = -100,000）：渲染舍入量级内页面
    # 放行，但同样的数若来自 API（精确值）就必须报——API 没有渲染舍入
    frame_api = pd.DataFrame([
        _row(datetime.date(2026, 3, 23), -217000000.0, -163000000.0,
             -54000000.0, 24000000.0, 192900000.0, -7.91, 0.89, 7.02),
    ])
    issues = ffs.consistency_violations(frame_api)
    assert len(issues) == 1 and "四档之和" in issues[0]
    assert ffs.consistency_violations(frame_api, rendered=True) == []


def test_page_genuine_decoy_still_caught_at_wan_scale():
    # 万粒度的行（金额 < 1 亿）容差仍是 1 万：09-11 那种扰动（主力 5617 万）
    # 走页面路径也一样要被咬住
    frame = pd.DataFrame([
        _row(datetime.date(2026, 7, 1), 56175000.0, 47753100.0, 8421900.0,
             3869700.0, -59968832.0),
    ])
    issues = ffs.consistency_violations(frame, rendered=True)
    assert len(issues) == 1 and "四档之和" in issues[0]


def test_page_ratio_violation_is_scale_independent():
    # 占比只有两位小数舍入，与金额量级无关：亿级行的占比扰动照样咬
    frame = pd.DataFrame([
        _row(datetime.date(2026, 3, 23), -217000000.0, -163000000.0,
             -54000000.0, 24000000.0, 193000000.0, 4.00, 0.50, -3.00),
    ])
    issues = ffs.consistency_violations(frame, rendered=True)
    assert any("占比之和" in i for i in issues)


# --- eastmoney_gateway：同一个接口，强制走付费网关 -------------------------------


def test_gateway_platform_is_registered_but_not_in_the_default_order():
    """付费回退必须显式配置才进链——默认顺序不变，不花冤枉钱。"""
    platform = pf.get("eastmoney_gateway")
    assert platform is not None and "fund_flow" in platform.capabilities
    assert "eastmoney_gateway" not in ffs.DEFAULT_PROVIDER_ORDER


def test_gateway_platform_replays_the_same_request_through_the_gateway(monkeypatch):
    """URL、参数、解析与主源完全一致——网关只是传输，不产生第二种数据形态。"""
    from finmcp.datasource import http_channel

    seen = {}

    def fake_gateway_request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return types.SimpleNamespace(
            json=lambda: {"rc": 0, "data": {"klines": [DELAY_KLINE]}})

    monkeypatch.setattr(http_channel, "gateway_request", fake_gateway_request)
    history = pf.get("eastmoney_gateway").fetch_fund_flow(REQUEST)

    assert seen["method"] == "GET"
    assert "push2his.eastmoney.com/api/qt/stock/fflow/daykline/get" in seen["url"]
    assert seen["params"]["secid"] == "1.000688"   # 沪市 ETF 的 secid 不能算错
    assert seen["params"]["lmt"] == "0" and seen["params"]["klt"] == "101"
    assert history.complete is True and history.rows == 1
    assert ffs._honours_contract(history) is True


def test_gateway_platform_returns_none_when_the_gateway_is_unavailable(monkeypatch):
    from finmcp.datasource import http_channel

    monkeypatch.setattr(http_channel, "gateway_request", lambda *a, **k: None)
    assert pf.get("eastmoney_gateway").fetch_fund_flow(REQUEST) is None


def test_gateway_platform_returns_none_for_an_empty_payload(monkeypatch):
    from finmcp.datasource import http_channel

    monkeypatch.setattr(
        http_channel, "gateway_request",
        lambda *a, **k: types.SimpleNamespace(json=lambda: {"rc": 100, "data": None}),
    )
    assert pf.get("eastmoney_gateway").fetch_fund_flow(REQUEST) is None


# --- satisfies：按查询需求判断"还要不要问下一个源" -------------------------------


def _history(rows: int, complete: bool, dates=None) -> ffs.FundFlowHistory:
    frame = _frame(rows)
    if dates is not None:
        import datetime as _dt
        frame["日期"] = [_dt.date.fromisoformat(d) for d in dates]
    return ffs.FundFlowHistory(frame=frame, complete=complete)


class TestSatisfies:
    def test_nothing_satisfies_nothing(self):
        assert ffs.satisfies(None, ffs.FundFlowNeed()) is False

    def test_complete_history_always_satisfies(self):
        # 全量历史定局：有就有，没有就是谁都没有。哪怕钉了一个非交易日，
        # 也不为"谁都没有"的日期再问下一个源（付费级更是纯亏）。
        need = ffs.FundFlowNeed(history_rows=60, pinned_date="2026-06-23")
        assert ffs.satisfies(_history(5, complete=True), need) is True

    def test_brief_realtime_stops_at_a_partial_frame(self):
        """实时 brief 的需求是"有一行"：delay 的当日行就满足，不碰链尾付费级。"""
        need = ffs.FundFlowNeed(history_rows=0)
        assert ffs.satisfies(_history(1, complete=False), need) is True

    def test_full_history_table_needs_the_rows(self):
        need = ffs.FundFlowNeed(history_rows=60)
        assert ffs.satisfies(_history(120, complete=False), need) is True   # 页面 120 行够
        assert ffs.satisfies(_history(1, complete=False), need) is False   # delay 一行不够

    def test_pinned_date_must_be_hit_exactly(self):
        need = ffs.FundFlowNeed(pinned_date="2026-06-23")
        frame_with = _history(3, complete=False, dates=["2026-06-19", "2026-06-22", "2026-06-23"])
        frame_without = _history(3, complete=False, dates=["2026-06-19", "2026-06-22", "2026-06-24"])
        assert ffs.satisfies(frame_with, need) is True
        assert ffs.satisfies(frame_without, need) is False

    def test_pinned_date_tolerates_string_date_columns(self):
        """页面兜底给的日期列是字符串，命中判定两种形态都要认。"""
        need = ffs.FundFlowNeed(pinned_date="2026-06-23")
        frame = _frame(2)
        frame["日期"] = ["2026-06-22", "2026-06-23"]
        assert ffs.satisfies(ffs.FundFlowHistory(frame=frame, complete=False), need) is True

    def test_full_pinned_today_still_needs_the_rows(self):
        """full 钉今天：delay 的当日单行命中日期但只有 1 行，历史表会缩成一行，
        不能算满足——行数需求在钉日期时同样成立（交集，不是二选一）。"""
        need = ffs.FundFlowNeed(history_rows=60, pinned_date="2026-09-15")
        delay_today = _history(1, complete=False, dates=["2026-09-15"])
        page_full = _history(120, complete=False, dates=["2026-09-15"] * 120)
        assert ffs.satisfies(delay_today, need) is False   # 命中日期但行数不够
        assert ffs.satisfies(page_full, need) is True      # 命中且行数够

    def test_brief_pinned_today_only_needs_the_date(self):
        """brief/medium 不渲染历史表，钉今天只需命中那一天，行数不是需求。"""
        need = ffs.FundFlowNeed(history_rows=0, pinned_date="2026-09-15")
        delay_today = _history(1, complete=False, dates=["2026-09-15"])
        assert ffs.satisfies(delay_today, need) is True


def test_resolve_stops_at_the_first_satisfying_source(registry):
    """need 让链在部分满足时停下：不再走到下一个源。"""
    partial = _Stub("partial", ffs.FundFlowHistory(frame=_frame(2), complete=False))
    gateway = _Stub("gateway", ffs.FundFlowHistory(frame=_frame(99), complete=True))
    pf.register(partial)
    pf.register(gateway)
    result = ffs.resolve(REQUEST, order=("partial", "gateway"),
                         need=ffs.FundFlowNeed(history_rows=0))
    assert result is not None and result.provider == "partial"
    assert gateway.calls == 0


def test_resolve_continues_when_the_need_is_not_met(registry):
    partial = _Stub("partial", ffs.FundFlowHistory(frame=_frame(2), complete=False))
    gateway = _Stub("gateway", ffs.FundFlowHistory(frame=_frame(99), complete=True))
    pf.register(partial)
    pf.register(gateway)
    result = ffs.resolve(REQUEST, order=("partial", "gateway"),
                         need=ffs.FundFlowNeed(history_rows=60))
    assert gateway.calls == 1
    assert result is not None and len(result.frame) == 99
