"""申万宏源研究所官网作为分级表的第二个源。

盯四件事：两级都从 JSON 里读出来、规则和 shenwan 一样；翻页在 count 到齐时停；TLS 先校验、
只在证书链不完整时放宽；以及它在链里的位置——乐咕乐股给不全时补、给全时一个请求都不发。
样例是 2026-09-06 从接口抄下来的字段形状。
"""

from __future__ import annotations

import logging

import pytest
import requests

from finmcp.datasource import platform as pf
from finmcp.datasource import sector_taxonomy as stx
from finmcp.datasource.platforms import swsresearch

REQUEST = stx.SectorTaxonomyRequest(sector_type="industry")


def _payload(names, count=None):
    return {"code": 0, "data": {
        "count": len(names) if count is None else count,
        "results": [{"swindexcode": f"80{i:04d}", "swindexname": n} for i, n in enumerate(names)],
    }}


def _serve(pages: dict, calls: list | None = None):
    """按 (indextype, page) 给页，记录请求参数。"""
    def get_json(params):
        if calls is not None:
            calls.append(dict(params))
        return pages.get((params["indextype"], params["page"]), _payload([], count=0))
    return get_json


# --- 解析 -------------------------------------------------------------------


def test_both_levels_are_read_from_the_official_api(monkeypatch):
    calls: list = []
    monkeypatch.setattr(swsresearch, "_get_json", _serve({
        ("一级行业", 1): _payload(["传媒", "电子"]),
        ("二级行业", 1): _payload(["证券Ⅱ", "半导体"]),
    }, calls))
    taxonomy = swsresearch.SwsResearchPlatform().fetch_sector_taxonomy(REQUEST)
    assert taxonomy.levels == {"传媒": 1, "电子": 1, "证券Ⅱ": 2, "半导体": 2}
    assert taxonomy.scheme == "shenwan" and taxonomy.source == "swsresearch"
    assert [c["indextype"] for c in calls] == ["一级行业", "二级行业"]
    assert all(c["page_size"] == swsresearch._PAGE_SIZE for c in calls)


def test_pages_are_followed_until_count_is_reached(monkeypatch):
    """接口哪天把页大小压回 50，少掉的是名单后半段，排名会安静地少一批板块——所以要翻。"""
    calls: list = []
    monkeypatch.setattr(swsresearch, "_get_json", _serve({
        ("一级行业", 1): _payload(["传媒", "电子"], count=3),
        ("一级行业", 2): _payload(["钢铁"], count=3),
        ("二级行业", 1): _payload(["证券Ⅱ"]),
    }, calls))
    taxonomy = swsresearch.SwsResearchPlatform().fetch_sector_taxonomy(REQUEST)
    assert taxonomy.names_at(1) == frozenset({"传媒", "电子", "钢铁"})
    assert [(c["indextype"], c["page"]) for c in calls] == [
        ("一级行业", 1), ("一级行业", 2), ("二级行业", 1)]


def test_a_name_in_both_levels_keeps_the_coarser_one(monkeypatch):
    monkeypatch.setattr(swsresearch, "_get_json", _serve({
        ("一级行业", 1): _payload(["综合"]),
        ("二级行业", 1): _payload(["综合", " 证券Ⅱ ", ""]),
    }))
    taxonomy = swsresearch.SwsResearchPlatform().fetch_sector_taxonomy(REQUEST)
    assert taxonomy.levels == {"综合": 1, "证券Ⅱ": 2}


def test_one_level_failing_still_yields_the_other(monkeypatch, caplog):
    def get_json(params):
        if params["indextype"] == "二级行业":
            raise requests.ConnectionError("boom")
        return _payload(["传媒"])

    monkeypatch.setattr(swsresearch, "_get_json", get_json)
    with caplog.at_level(logging.WARNING, logger="finmcp"):
        taxonomy = swsresearch.SwsResearchPlatform().fetch_sector_taxonomy(REQUEST)
    assert taxonomy.levels == {"传媒": 1}
    assert "申万宏源研究所2级行业分类取数失败" in caplog.text


def test_nothing_returned_is_none(monkeypatch):
    monkeypatch.setattr(swsresearch, "_get_json", _serve({}))
    assert swsresearch.SwsResearchPlatform().fetch_sector_taxonomy(REQUEST) is None


def test_only_industries_are_supported():
    platform = pf.get("swsresearch")
    assert platform.supports(stx.CAPABILITY, REQUEST) is True
    assert platform.supports(stx.CAPABILITY, stx.SectorTaxonomyRequest("concept")) is False


# --- TLS --------------------------------------------------------------------


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_tls_is_verified_first_and_relaxed_only_on_ssl_error(monkeypatch):
    """服务器不发中间证书，certifi 校验必失败；放宽只发生在这一种错误之后。"""
    seen: list = []

    def get(url, **kwargs):
        seen.append(kwargs.get("verify", True))
        if len(seen) == 1:
            raise requests.exceptions.SSLError("certificate verify failed")
        return _Response(_payload(["传媒"]))

    monkeypatch.setattr(requests, "get", get)
    payload = swsresearch._get_json({"page": 1, "page_size": 200, "indextype": "一级行业"})
    assert payload["data"]["count"] == 1
    assert seen == [True, False]


def test_other_errors_are_not_retried_unverified(monkeypatch):
    seen: list = []

    def get(url, **kwargs):
        seen.append(kwargs.get("verify", True))
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(requests.ConnectionError):
        swsresearch._get_json({"page": 1, "page_size": 200, "indextype": "一级行业"})
    assert seen == [True]


# --- 在链里的位置 -------------------------------------------------------------


def _tax(levels, source):
    return stx.SectorTaxonomy(levels=levels, scheme="shenwan", source=source)


@pytest.fixture
def chain(monkeypatch):
    """两个真平台的 fetch 换成可控的，顺序用默认的。"""
    monkeypatch.delenv("SECTOR_TAXONOMY_PROVIDERS", raising=False)
    patched: list = []

    def install(shenwan, sws):
        for name, fetch in (("shenwan", shenwan), ("swsresearch", sws)):
            platform = pf.get(name)
            platform.fetch_sector_taxonomy = fetch
            patched.append(platform)

    yield install
    for platform in patched:
        del platform.fetch_sector_taxonomy


def test_a_complete_first_source_stops_the_chain(chain):
    chain(lambda request: _tax({"传媒": 1, "证券Ⅱ": 2}, "shenwan"),
          lambda request: pytest.fail("乐咕乐股给全了，不该再问官网"))
    taxonomy = stx._fetch("industry")
    assert taxonomy.source == "shenwan"
    assert taxonomy.levels == {"传媒": 1, "证券Ⅱ": 2}


def test_the_second_source_fills_the_level_the_first_could_not(chain):
    """乐咕乐股二级那页坏了：一级留着、二级由官网补，同名冲突信先配置的。"""
    chain(lambda request: _tax({"传媒": 1, "综合": 1}, "shenwan"),
          lambda request: _tax({"传媒": 1, "综合": 2, "证券Ⅱ": 2}, "swsresearch"))
    taxonomy = stx._fetch("industry")
    assert taxonomy.levels == {"传媒": 1, "综合": 1, "证券Ⅱ": 2}
    assert taxonomy.source == "shenwan+swsresearch"


def test_when_the_first_source_is_blocked_the_second_serves_alone(chain):
    """机房出口 IP 的情形：乐咕乐股回人机验证页，两级全挂。"""
    chain(lambda request: None,
          lambda request: _tax({"传媒": 1, "证券Ⅱ": 2}, "swsresearch"))
    taxonomy = stx._fetch("industry")
    assert taxonomy.source == "swsresearch" and taxonomy.level_of("证券Ⅱ") == 2


def test_half_a_table_is_still_returned_when_no_one_can_complete_it(chain):
    chain(lambda request: _tax({"传媒": 1}, "shenwan"), lambda request: None)
    assert stx._fetch("industry").levels == {"传媒": 1}


def test_tables_of_different_schemes_are_not_merged():
    base = stx.SectorTaxonomy(levels={"传媒": 1}, scheme="shenwan", source="a")
    other = stx.SectorTaxonomy(levels={"证券Ⅱ": 2}, scheme="zhongxin", source="b")
    assert stx._merge(base, other) is base
    assert stx._merge(None, other) is other


def test_the_default_order_tries_the_official_site_second():
    assert stx.DEFAULT_PROVIDER_ORDER == ("shenwan", "swsresearch")
    assert pf.get("swsresearch") is not None
