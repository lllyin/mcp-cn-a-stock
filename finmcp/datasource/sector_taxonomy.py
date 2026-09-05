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

分类一年动一两次，所以缓存 24 小时，对每次查询是零成本。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Optional

from ..config import SECTOR_TAXONOMY_TTL_SECONDS
from . import platform as pf

logger = logging.getLogger("finmcp")

CAPABILITY = "sector_taxonomy"
PROVIDER_ORDER_ENV = "SECTOR_TAXONOMY_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("shenwan",)

#: 排名默认排哪一级。一级是"哪个方向在被买"这个问题的粒度，31 个也正好排得开。
TOP_LEVEL = 1


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

_lock = threading.Lock()
_cached: dict = {}


def load(sector_type: str = "industry", *, force: bool = False) -> Optional[SectorTaxonomy]:
    """取一份分级表，进程内按 ``sector_type`` 缓存。

    取不到返回 None，调用方按"分不出层级"处理。这一层不抛异常——分级是给排名用的
    辅助信息，它挂了不该让板块资金流整个查不出来。
    """
    now = time.monotonic()
    with _lock:
        entry = _cached.get(sector_type)
        if not force and entry is not None and now - entry[1] < SECTOR_TAXONOMY_TTL_SECONDS:
            return entry[0]

    order = pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)
    resolved = pf.resolve(CAPABILITY, SectorTaxonomyRequest(sector_type=sector_type), order=order)
    taxonomy = resolved.value if resolved is not None else None

    with _lock:
        _cached[sector_type] = (taxonomy, time.monotonic())
    if taxonomy is not None:
        logger.debug("板块分级 sector_type=%s 标准=%s 覆盖=%s 个",
                     sector_type, taxonomy.scheme, len(taxonomy.levels))
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
    """清掉进程内缓存。给测试和排查用。"""
    with _lock:
        _cached.clear()


__all__ = [
    "CAPABILITY",
    "applies_to",
    "DEFAULT_PROVIDER_ORDER",
    "PROVIDER_ORDER_ENV",
    "TOP_LEVEL",
    "SectorTaxonomy",
    "SectorTaxonomyRequest",
    "load",
    "reset_cache",
]
