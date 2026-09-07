"""scripts/probe_tuning.py 的纯逻辑：逐 tab 统计、四条判据、.env 比对、可达性归类、盘中守卫、
隔离实例的前缀环境。不起浏览器、不联网。

样例的形状取自真实日志：一种身份首 tab 16%、第 2 个 tab 条件恢复 19%、第 3 个 5%，批内首 tab
成功率前半 3/8 后半 0/8；另一种身份 19/19。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "probe_tuning", Path(__file__).resolve().parents[1] / "scripts" / "probe_tuning.py"
)
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["probe_tuning"] = probe
_SPEC.loader.exec_module(probe)


# ── 造样例 ─────────────────────────────────────────────────────────


def _record(identity, rnd, symbol, tab, outcome, started, service=3.0, refused_blocks=()):
    return {
        "identity": identity, "round": rnd, "symbol": symbol, "tab": tab, "outcome": outcome,
        "service_s": service, "started": started, "refused_blocks": list(refused_blocks),
        "history_rows": 120 if outcome == "today" else 0, "captcha": outcome == "refused_captcha",
    }


def _arm(identity, sequences, t0=1000.0, service_ok=3.0):
    """sequences：每个 episode 的逐 tab 结果，如 ["refused_captcha", "today"]。按时间顺序排开。"""
    records = []
    clock = t0
    for index, sequence in enumerate(sequences):
        for tab, outcome in enumerate(sequence, start=1):
            service = 1.5 if outcome.startswith("refused") else service_ok
            records.append(_record(identity, index // 4, f"60{index:04d}", tab, outcome, clock, service))
            clock += 2.0
    return records


ALL_OK = [["today"]] * 12
# 12 个：2 个首 tab 成功；4 个第 2 个 tab 救回；6 个三个 tab 全被拒。
MORNING = ([["today"]] * 2
           + [["refused_captcha", "today"]] * 4
           + [["refused_captcha", "refused_captcha", "refused_captcha"]] * 6)


def _summary(*arms):
    records = []
    for arm in arms:
        records.extend(arm)
    return probe.summarise_browser(records)


# ── 逐 tab 统计 ────────────────────────────────────────────────────


def test_summary_reconstructs_first_tab_and_conditional_recovery():
    summary = _summary(_arm("legacy", ALL_OK), _arm("disguise", MORNING))
    legacy, disguise = summary["legacy"], summary["disguise"]
    assert (legacy["episodes"], legacy["first_tab_ok"], legacy["final_ok"]) == (12, 12, 12)
    assert legacy["loads"] == 12 and legacy["refused_tabs"] == 0
    assert (disguise["episodes"], disguise["first_tab_ok"]) == (12, 2)
    assert disguise["reached"] == {1: 12, 2: 10, 3: 6}
    assert disguise["success_at"] == {1: 2, 2: 4, 3: 0}
    assert disguise["recovery"] == {2: 0.4, 3: 0.0}
    assert disguise["final_ok"] == 6 and disguise["loads"] == 2 + 4 * 2 + 6 * 3
    assert disguise["captcha_tabs"] == 4 + 18
    assert disguise["service_refused_s"]["p50"] == 1.5 and disguise["service_ok_s"]["n"] == 6


def test_summary_measures_decline_within_the_run():
    """前半 4/6 首 tab、后半 0/6：这是"重试在喂风控"的形状。"""
    sequences = [["today"]] * 4 + [["refused_captcha", "refused_captcha", "refused_captcha"]] * 8
    arm = _summary(_arm("disguise", sequences))["disguise"]
    assert arm["first_half_rate"] == pytest.approx(4 / 6, abs=1e-3)
    assert arm["second_half_rate"] == 0.0
    assert arm["decline"] == pytest.approx(4 / 6, abs=1e-3)


def test_a_refused_today_block_counts_as_refused_and_ends_the_episode():
    """历史到了、今日被接口拒：线上不换 tab，这里同样不算到达第 2 个 tab，但它是被拒不是没数据。"""
    records = [_record("legacy", 0, "600519", 1, "refused_today", 1000.0, 7.4, ("today",)),
               _record("legacy", 0, "000333", 1, "today", 1002.0, 7.2, ("history",))]
    arm = probe.summarise_browser(records)["legacy"]
    assert (arm["episodes"], arm["first_tab_ok"], arm["refused_tabs"], arm["nodata_episodes"]) == (2, 1, 1, 0)
    assert arm["reached"] == {1: 2}
    assert arm["refused_blocks"] == {"today": 1, "history": 1}


# ── 身份判据 ───────────────────────────────────────────────────────


def test_disguise_stays_off_when_the_plain_identity_wins_by_the_margin():
    decision = probe.decide_disguise(_summary(_arm("legacy", ALL_OK), _arm("disguise", MORNING)))
    assert (decision.value, decision.status) == ("0", "measured")
    assert "原样 12/12" in decision.evidence and "伪装 2/12" in decision.evidence


def test_disguise_switches_on_only_when_it_wins_by_the_margin():
    legacy = _arm("legacy", [["today"]] * 6 + [["refused", "refused", "refused"]] * 6)
    decision = probe.decide_disguise(_summary(legacy, _arm("disguise", ALL_OK)))
    assert (decision.value, decision.status) == ("1", "measured")


def test_disguise_needs_twelve_episodes_per_arm():
    decision = probe.decide_disguise(_summary(_arm("legacy", ALL_OK[:8]), _arm("disguise", MORNING[:8])))
    assert (decision.value, decision.status) == ("0", "inconclusive")


def test_disguise_within_margin_keeps_the_default():
    disguise = _arm("disguise", [["today"]] * 11 + [["refused", "today"]])
    decision = probe.decide_disguise(_summary(_arm("legacy", ALL_OK), disguise))
    assert (decision.value, decision.status) == ("0", "default")


def test_disguise_without_a_control_arm_is_unmeasured():
    decision = probe.decide_disguise(_summary(_arm("legacy", ALL_OK)))
    assert (decision.value, decision.status) == ("0", "unmeasured")


# ── 重试判据 ───────────────────────────────────────────────────────


def test_max_loads_is_two_when_the_first_tab_saturates():
    arm = _summary(_arm("legacy", ALL_OK))["legacy"]
    decision = probe.decide_max_loads(arm, 3)
    assert (decision.value, decision.status) == ("2", "measured")
    assert "用不到" in decision.evidence


def test_max_loads_follows_the_recovery_curve():
    """第 2 个 tab 恢复 40%（到达 10）、第 3 个 0%：允许到第 2 个。"""
    arm = _summary(_arm("disguise", MORNING))["disguise"]
    assert probe.decide_max_loads(arm, 3).value == "2"
    # 第 3 个 tab 也救得回（4/12 = 33%），且批内没衰减：允许到第 3 个。
    sequences = []
    for i in range(24):
        if i % 6 == 0:
            sequences.append(["today"])
        elif i % 6 in (1, 2):
            sequences.append(["refused_captcha", "today"])
        elif i % 6 == 3:
            sequences.append(["refused_captcha", "refused_captcha", "today"])
        else:
            sequences.append(["refused_captcha", "refused_captcha", "refused_captcha"])
    arm = _summary(_arm("disguise", sequences))["disguise"]
    assert arm["reached"] == {1: 24, 2: 20, 3: 12} and arm["success_at"][3] == 4
    decision = probe.decide_max_loads(arm, 3)
    assert (decision.value, decision.status) == ("3", "measured")


def test_max_loads_does_not_grow_when_retries_feed_the_gate():
    """前半 4/6、后半 0/6：哪怕第 2 个 tab 的条件恢复率够，也不多给。"""
    sequences = [["today"]] * 4 + [["refused_captcha", "today"]] * 3 + [["refused_captcha"] * 3] * 5
    arm = _summary(_arm("disguise", sequences))["disguise"]
    assert arm["recovery"][2] == pytest.approx(3 / 8)
    decision = probe.decide_max_loads(arm, 3)
    assert decision.value == "2" and "喂风控" in decision.evidence


def test_max_loads_needs_enough_episodes():
    arm = _summary(_arm("disguise", MORNING[:6]))["disguise"]
    assert probe.decide_max_loads(arm, 3).status == "inconclusive"
    assert probe.decide_max_loads(None, 3).value == "2"


# ── 等待判据 ───────────────────────────────────────────────────────


def _service_arm(p90, p50=None, n=20):
    return {"service_ok_s": {"n": n, "p50": p50 or round(p90 / 2, 2), "p90": p90, "max": p90 + 1}}


def test_queue_wait_reproduces_the_deploy_box_numbers():
    """p90 7.49 s 推出 8 / 15，正是 config.py 里的默认值。"""
    wait, budget = probe.decide_queue_wait(_service_arm(7.49))
    assert (wait.value, budget.value) == ("8", "15")
    assert wait.status == "default"          # 与默认相差不到 2 秒，不改


def test_queue_wait_grows_with_a_slow_machine_and_is_capped():
    wait, budget = probe.decide_queue_wait(_service_arm(12.0))
    assert (wait.value, budget.value, wait.status) == ("13", "25", "measured")
    wait, budget = probe.decide_queue_wait(_service_arm(30.0))
    assert (wait.value, budget.value) == ("15", "45" if False else str(min(40, 15 + 30)))


def test_queue_wait_shrinks_on_a_fast_machine():
    wait, budget = probe.decide_queue_wait(_service_arm(2.3))
    assert (wait.value, budget.value) == ("3", "6")


def test_queue_wait_needs_ten_successful_loads():
    wait, budget = probe.decide_queue_wait(_service_arm(12.0, n=9))
    assert (wait.value, budget.value, wait.status) == ("8", "15", "inconclusive")


# ── 页数判据 ───────────────────────────────────────────────────────


def _ladder(pss_by_pages, rss_by_pages=None):
    rows = [{"pages": 0, "rss_mib": 100.0, "pss_mib": 60.0, "browser_delta_rss_mib": 0.0, "browser_delta_pss_mib": 0.0}]
    for pages, pss in sorted(pss_by_pages.items()):
        rss = (rss_by_pages or {}).get(pages, (pss or 0) * 1.8)
        rows.append({"pages": pages, "rss_mib": 100 + rss, "pss_mib": None if pss is None else 60 + pss,
                     "browser_delta_rss_mib": rss, "browser_delta_pss_mib": pss})
    return {"ladder": rows}


def test_max_pages_from_pss_headroom():
    pages, conc = probe.decide_max_pages(_ladder({1: 200.0, 2: 330.0, 3: 460.0}), 243.0, "线上服务")
    # (500 - 243 - 200) / 130 = 0.4 → 放得下 1 页
    assert (pages.value, conc.value, pages.status) == ("1", "1", "measured")
    pages, _ = probe.decide_max_pages(_ladder({1: 150.0, 2: 200.0, 3: 250.0}), 243.0, "线上服务")
    assert pages.value == "3"                # 107 / 50 = 2 → 1 + 2 = 3


def test_max_pages_is_unmeasured_without_pss():
    pages, conc = probe.decide_max_pages(_ladder({1: None, 2: None, 3: None}, {1: 378.0, 2: 505.0, 3: 642.0}), None, "文档值")
    assert (pages.value, pages.status) == ("3", "unmeasured")
    assert "RSS" in pages.evidence and conc.value == "3"


def test_max_pages_falls_back_to_the_documented_base():
    pages, _ = probe.decide_max_pages(_ladder({1: 150.0, 2: 200.0, 3: 250.0}), None, "文档值")
    assert pages.value == "3" and "文档值" in pages.evidence


# ── .env 比对 ──────────────────────────────────────────────────────


def _decisions(**overrides):
    base = {
        "BROWSER_DISGUISE": probe.Decision("BROWSER_DISGUISE", "0", "measured", "原样 24/24，伪装 15/24"),
        "FUND_FLOW_PAGE_MAX_LOADS": probe.Decision("FUND_FLOW_PAGE_MAX_LOADS", "2", "measured", "首 tab 100%"),
        "BROWSER_MAX_PAGES": probe.Decision("BROWSER_MAX_PAGES", "3", "unmeasured", "无 PSS"),
    }
    base.update(overrides)
    return base


def test_lint_flags_debug_residue_and_the_morning_env():
    values = {"BROWSER_HEADFUL": "1", "BROWSER_KEEP_PAGES": "1", "FUND_FLOW_PAGE_MAX_LOADS": "5",
              "FUND_FLOW_PAGE_COOLDOWN_SECONDS": "20", "BROWSER_DISGUISE": "1", "CN_STOCK_FETCH_MAX_WORKERS": "8"}
    findings = probe.lint_env(values, _decisions())
    keys = [f["key"] for f in findings]
    assert keys[:4] == ["BROWSER_DISGUISE", "BROWSER_HEADFUL", "BROWSER_KEEP_PAGES", "FUND_FLOW_PAGE_MAX_LOADS"]
    assert {f["key"]: f["level"] for f in findings}["FUND_FLOW_PAGE_COOLDOWN_SECONDS"] == "medium"
    assert {f["key"]: f["level"] for f in findings}["CN_STOCK_FETCH_MAX_WORKERS"] == "medium"
    assert len(findings) == 6


def test_lint_is_quiet_for_a_matching_env_and_marks_redundant_defaults():
    assert probe.lint_env({"BROWSER_DISGUISE": "0", "FUND_FLOW_PAGE_MAX_LOADS": "2"}, _decisions()) == []
    findings = probe.lint_env({"BROWSER_MAX_PAGES": "3"}, _decisions())
    assert findings == [] or all(f["level"] == "low" for f in findings)
    findings = probe.lint_env({"BROWSER_MAX_PAGES": "2"}, _decisions())
    assert findings[0]["level"] == "medium" and "未能判定" in findings[0]["finding"]


def test_render_env_carries_the_evidence_above_each_line():
    text = probe.render_env(list(_decisions().values()), {"generated": "2026-09-07 15:20", "hostname": "box"})
    assert "# [实测] 原样 24/24，伪装 15/24\nBROWSER_DISGUISE=0" in text
    assert "# [未测，保持默认] 无 PSS\nBROWSER_MAX_PAGES=3" in text
    assert "不要整份覆盖" in text


def test_build_decisions_survives_a_json_round_trip():
    browser = {"summary": _summary(_arm("legacy", ALL_OK), _arm("disguise", MORNING)), "max_tabs": 3, "memory": {}}
    browser = json.loads(json.dumps(browser))          # 键变成字符串，和从 browser.json 读出来一样
    decisions = {d.key: d for d in probe.build_decisions(None, browser)}
    assert decisions["BROWSER_DISGUISE"].value == "0"
    assert decisions["FUND_FLOW_PAGE_MAX_LOADS"].value == "2"
    assert decisions["BROWSER_MAX_PAGES"].status == "unmeasured"
    assert decisions["HTTP_CHANNEL"].value == "auto"


def test_report_renders_with_partial_inputs():
    text = probe.render_report(facts=None, browser=None, decisions=probe.build_decisions(None, None),
                               lint=[], notes=probe.channel_notes(None), meta={"generated": "t", "hostname": "h"})
    assert "## 一、推荐配置" in text and "没有浏览器探测数据" in text


# ── 文案不绑定某台机器 ─────────────────────────────────────────────


def test_user_facing_strings_do_not_cite_another_machine_or_a_date():
    """开源项目：报告、lint、依据里只能有这台机器的测量和机制说明，不能引用别处的测试结果。
    模块 docstring 是设计依据，允许写实测数字，但同样不点名机器。"""
    import ast
    import re

    tree = ast.parse(Path(probe.__file__).read_text(encoding="utf-8"))
    body = ast.Module(body=tree.body[1:], type_ignores=[])       # 跳过模块 docstring
    strings = [node.value for node in ast.walk(body)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    offenders = [s for s in strings
                 if "部署机" in s or "本机" in s                       # noqa: machine-coupling
                 or re.search(r"20\d\d-\d\d", s)]
    assert offenders == []
    assert "部署机" not in ast.get_docstring(tree)     # noqa: machine-coupling


# ── 可达性归类 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status,location,text,error,kind,expected", [
    (200, None, '{"rc":0,"data":{"diff":[{"f12":"000001"}]}}', None, "json", "ok"),
    (200, None, '{"rc":0,"data":null}', None, "json", "empty"),
    (302, "https://legulegu.com/human-challenge?x=1", "", None, "html", "blocked"),
    (302, "https://example.com/other", "", None, "html", "redirect_302"),
    (403, None, "", None, "json", "blocked"),
    (502, None, "<html>502</html>", None, "jsonp", "http_502"),
    (None, None, "", "ConnectionError: curl: (56) Connection closed abruptly", "json", "refused"),
    (None, None, "", "URLError: <urlopen error [Errno 54] Connection reset by peer>", "json", "refused"),
    (None, None, "", "TimeoutError: timed out", "json", "timeout"),
    (None, None, "", "URLError: CERTIFICATE_VERIFY_FAILED", "json", "tls_error"),
    (200, None, 'v_sh000001="1~上证指数~000001~3800.00"', None, "text:v_sh000001=", "ok"),
    (200, None, 'quotebridge_v6_line_hs_1A0001_01_2026({"data":"..."})', None, "jsonp", "ok"),
])
def test_classify_http(status, location, text, error, kind, expected):
    assert probe.classify_http(status, location, text, error, kind) == expected


# ── 盘中守卫 ───────────────────────────────────────────────────────


def test_guard_refuses_trading_hours_only():
    trading = lambda day: day.weekday() < 5
    monday = dt.date(2026, 9, 7)
    assert probe.in_trading_session(dt.datetime.combine(monday, dt.time(10, 0)), trading)
    assert probe.in_trading_session(dt.datetime.combine(monday, dt.time(9, 15)), trading)
    assert not probe.in_trading_session(dt.datetime.combine(monday, dt.time(9, 14)), trading)
    assert not probe.in_trading_session(dt.datetime.combine(monday, dt.time(12, 0)), trading)
    assert not probe.in_trading_session(dt.datetime.combine(monday, dt.time(15, 5)), trading)
    saturday = dt.date(2026, 9, 5)
    assert not probe.in_trading_session(dt.datetime.combine(saturday, dt.time(10, 0)), trading)


# ── verify 的前缀环境 ──────────────────────────────────────────────


def test_prefixed_environment_moves_dotenv_behind_the_prefix(tmp_path):
    env = probe.prefixed_environment({"PATH": "/bin"}, {"AKSHARE_PROXY_TOKEN": "t", "BROWSER_DISGUISE": "1"},
                                     {"BROWSER_DISGUISE": "0"}, "PROBE_", tmp_path / "cache")
    assert env["ENV_PREFIX"] == "PROBE_"
    assert env["PROBE_AKSHARE_PROXY_TOKEN"] == "t"
    assert env["PROBE_BROWSER_DISGUISE"] == "0"           # 候选盖住 .env
    assert env["PROBE_CACHE_DIR"] == str(tmp_path / "cache")
    assert "BROWSER_DISGUISE" not in env                   # 裸名字一个都不给，.env 灌进去的也读不到


def test_prefixed_environment_refuses_a_dotenv_that_sets_its_own_prefix(tmp_path):
    with pytest.raises(SystemExit):
        probe.prefixed_environment({}, {"ENV_PREFIX": "X_"}, {}, "PROBE_", tmp_path)
