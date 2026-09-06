"""板块分级：一个板块名在行业分类里是第几级。

## 为什么需要它

东财的行业板块 ``m:90 t:2`` 是**一棵树摊平成的一张名单**——2026-09-05 实测 496 个，
里面同时有申万一级（传媒、钢铁）、二级（证券Ⅱ、保险Ⅱ）和三级（证券Ⅲ、种子）。
两个东财端点（push2 与 dataapi）返回的是同一批 496 个，不存在源之间的分歧。

问题出在**排名**上：对一棵树取 Top N，父子会同时上榜，同一笔钱数两遍。
2026-09-05 实测，"净流出前 10"里 电子 -817.95亿 和它的子板块 半导体 -602.46亿
各占一格，证券Ⅱ 与 证券Ⅲ 都是 32.35亿 又占两格——十行里没有十个独立的板块。

## 为什么单独做成一个能力

东财这批数据里**没有层级字段**：``f12`` 的 BK0/BK1 前缀不是层级（BK0 里有一级的
"传媒"也有二级的"证券Ⅱ"，BK1 里有一级的"非银金融"），名字后缀 Ⅱ/Ⅲ 只有 496 个
里的 82 个带。按前缀或按后缀去猜都是特殊逻辑，换一个源就废。

所以层级从**分类标准本身**取：申万宏源发布的行业分类。它和资金流是两码事、来自
另一个上游、变化频率也完全不同（分类一年动一两次，资金流每天变），正好各自成一个
能力。取不到就 ``level=None``，渲染层如实说"这一批分不出层级"，而不是假装排好了。

## 实测

- ``sw_index_first_info`` 31 个一级行业，1.15s，和东财板块名 **31/31 全部对上**
- ``sw_index_second_info`` 131 个二级，1.22s，对上 127 个（97%）；对不上的 4 个是
  银行的细分（农商行Ⅱ、国有大型银行Ⅱ、城商行Ⅱ、股份制银行Ⅱ）
- ``sw_index_third_info`` 三级是 HTML 抓取，实测 4 次成 2 次——**不接它**。
  排名只需要一个干净的层，一级已经够，为它引入一个一半会失败的依赖不划算。

## 为什么有第二个源

``sw_index_*_info`` 抓的是乐咕乐股的页面，它前面的阿里云 WAF 对机房 IP 回 302 跳到
人机验证页：部署机 2026-09-06 两级全挂（``'NoneType' object has no attribute 'find_all'``），
本机同一时刻 200。同一段代码一台机器有分级一台没有，榜就对不上东财官网。第二个源
``swsresearch`` 直接调申万宏源研究所官网的 JSON 接口，同一套标准：一级 31/31 同名，
二级 124 个全部同名、比乐咕少 7 个小板块（细节见 platforms/swsresearch.py）。

两个源同一套标准，所以**合并**而不是二选一（``_merge``）：前一个缺的级由后一个补——
乐咕是 HTML 抓取，两级各自成败，一级到了二级没到的情形真实存在；同名冲突信先配置的。
``_enough`` 要求能排的每一级都有名字，乐咕给全时官网一个请求都不发。

分类一年动一两次，所以缓存 24 小时，对每次查询是零成本。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional

from .. import cache
from ..config import CACHE_TAXONOMY_TTL_SECONDS
from . import platform as pf

logger = logging.getLogger("finmcp")

CAPABILITY = "sector_taxonomy"
PROVIDER_ORDER_ENV = "SECTOR_TAXONOMY_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("shenwan", "swsresearch")

#: 排名默认排哪一级。**二级**——判据是东财官网自己就这么排：
#: data.eastmoney.com/bkzj/hy.html 的"行业板块资金流向排行"共 3 页 × 50 行，
#: 2026-09-05 抓下来的三页 HTML 里，第一页 50 个板块**全部**是申万二级，一个一级
#: 都没有（传媒当日 61.74亿 是全场最大，但它是一级，页面上根本没有它）。
#: 东财 496 个板块按申万分：一级 31、二级 127、其余 338 为三级。
#:
#: 排一级也不算错（不重不漏），但 31 个太粗，而且和用户在东财、券商 App 上看到的
#: 那张榜对不上——对不上就得解释，解释不清就会被当成数据错了。
DEFAULT_RANK_LEVEL = 2
#: 能排的层级。三级不进来：sw_index_third_info 是 HTML 抓取，实测 4 次成 2 次。
RANK_LEVELS = (1, 2)


@dataclass(frozen=True)
class SectorTaxonomyRequest:
    sector_type: str = "industry"


@dataclass(frozen=True)
class SectorTaxonomy:
    """板块名 → 层级。

    ``scheme`` 记的是按谁的分类标准分的（``shenwan``），要写进报告——不同标准分出来
    的层级不一样，不说清楚等于没说。名单里没有的板块是"这个标准不认它"，
    不是"它没有层级"，所以查询返回 None 而不是猜一个。
    """

    levels: Mapping[str, int]
    scheme: str
    source: str = ""

    def level_of(self, name: str) -> Optional[int]:
        return self.levels.get((name or "").strip())

    def names_at(self, level: int) -> frozenset:
        return frozenset(n for n, lv in self.levels.items() if lv == level)


pf.define_capability(CAPABILITY, SectorTaxonomy)


# ── 缓存 ────────────────────────────────────────────────────────
#
# TTL 型：行业分类和交易时段无关，一年才动一两次。落盘的理由是有效期以天计，
# 而进程重启是分钟级的事——不落盘等于每次重启都重付一次上游（31+131 行，约 2.4s）。

CACHE_NAMESPACE = "taxonomy"

cache.register_namespace(cache.Namespace(
    name=CACHE_NAMESPACE,
    max_entries=4,
    epoch_bound=False,
    ttl_seconds=CACHE_TAXONOMY_TTL_SECONDS,
    # 一年才动一两次，旧一个月和新的一模一样。
    max_age_seconds=30 * 86400,
    disk=True,
    encode=lambda tax: {"levels": dict(tax.levels), "scheme": tax.scheme,
                        "source": tax.source},
    decode=lambda payload: SectorTaxonomy(
        levels={k: int(v) for k, v in payload["levels"].items()},
        scheme=payload.get("scheme", ""), source=payload.get("source", "")),
))


def _merge(base: Optional[SectorTaxonomy], extra: SectorTaxonomy) -> SectorTaxonomy:
    """把后一个源的层级合进前一个。

    解的是"A 缺二级、B 有二级"。同一个名字两个源给的层级不同，信先配置的那个——
    那是仲裁，归顺序管，不归合并管。分类标准不同的表不合：申万的二级和别家的二级
    不是一回事，合了就是错的，这时只留前面那份。
    """
    if base is None:
        return extra
    if extra.scheme != base.scheme:
        return base
    levels = dict(extra.levels)
    levels.update(base.levels)
    return SectorTaxonomy(levels=levels, scheme=base.scheme,
                          source=f"{base.source}+{extra.source}")


def _enough(taxonomy: SectorTaxonomy) -> bool:
    """能排的每一级都有名字才算够；差一级就继续问下一个源。"""
    present = set(taxonomy.levels.values())
    return all(level in present for level in RANK_LEVELS)


def _fetch(sector_type: str) -> Optional[SectorTaxonomy]:
    order = pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)
    resolved = pf.resolve(
        CAPABILITY, SectorTaxonomyRequest(sector_type=sector_type), order=order,
        merge=_merge, enough=_enough)
    return resolved.value if resolved is not None else None


def load(sector_type: str = "industry", *, force: bool = False) -> Optional[SectorTaxonomy]:
    """取一份分级表。

    取不到返回 None，调用方按"分不出层级"处理。这一层不抛异常——分级是给排名用的
    辅助信息，它挂了不该让板块资金流整个查不出来。单飞和旧值兜底由缓存层内建。
    """
    if force:
        cache.cache_for(CACHE_NAMESPACE).clear()
    entry = cache.get_or_load(
        CACHE_NAMESPACE, sector_type, lambda: _fetch(sector_type))
    taxonomy = None if entry is None else entry.value
    if taxonomy is not None:
        logger.debug("板块分级 sector_type=%s 标准=%s 来源=%s 覆盖=%s 个 新鲜=%s",
                     sector_type, taxonomy.scheme, taxonomy.source,
                     len(taxonomy.levels), entry.fresh)
    return taxonomy


def applies_to(sector_type: str) -> bool:
    """这个板块类型有没有分级标准可用。

    概念和地域板块**本来就没有层级**——申万不分它们，别的分类标准也不分。这和
    "行业有层级但这次没取到"是两回事：前者不该报警，后者该。纯计算，不发请求。
    """
    order = pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)
    request = SectorTaxonomyRequest(sector_type=sector_type)
    for name in order:
        platform = pf.get(name)
        if platform is not None and platform.supports(CAPABILITY, request):
            return True
    return False


def reset_cache() -> None:
    """清掉缓存。给测试和排查用。"""
    cache.cache_for(CACHE_NAMESPACE).clear()


__all__ = [
    "CAPABILITY",
    "applies_to",
    "DEFAULT_PROVIDER_ORDER",
    "PROVIDER_ORDER_ENV",
    "DEFAULT_RANK_LEVEL",
    "RANK_LEVELS",
    "SectorTaxonomy",
    "SectorTaxonomyRequest",
    "load",
    "reset_cache",
]
