"""把服务自己的日志读成结构化的健康数据。``health`` 工具的取数层。

**不发任何上游请求。** 只读本进程写下的日志,所以零积分、零上游压力、随时可调,
也不会因为"查健康"反过来把服务打挂。

## 时间窗怎么切

``start.sh`` 每次启动都 ``mv cn-stock-mcp.log cn-stock-mcp.log.bak``,所以当前那个
文件天然就是"本次启动至今"——``since=startup`` 不需要在文件里找启动标记,读它就是。
要跨重启(``today``)才需要把 ``.bak`` 也读进来。

## 可用率的分母

分母不是"调用次数",是**维度实例数**:每个完成的标的按 ``report_contract`` 算出它
在那个工具下应该有哪些维度,ETF 没有财务、指数没有市值不进分母。分子由渲染时写下的
``present=N/M`` 直接给出——那是实测的,不是按上游失败反推的。

老日志没有 ``present=`` 字段时退回反推(``incomplete_sources=<源>`` → 该源名下的
维度),此时 ``availability.method`` 是 ``inferred``,读的人要知道它看不见"源成功
返回但字段为空"那一类。
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .report_contract import CONTRACT, expected

#: 一次最多读多少字节。日志一天能到几 MB，长期跑更大；这个工具自己绝不能变成
#: 性能问题。超了就从尾部截断并在 warnings 里说明。
MAX_BYTES = 8 * 1024 * 1024

_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

_RE_SYMBOL = re.compile(
    _TS + r".*Finished symbol request_id=(\S+) tool=(\S+) symbol=(\S+) "
    r"raw_data=([\d.]+)s render=([\d.]+)s total=([\d.]+)s chars=(\d+)"
    r"(?: present=(\d+)/(\d+) missing=(\S*) degraded=(\S*))?")
_RE_TASK = re.compile(_TS + r".*Data task (\S+) .*?symbol=(\S+) .*?service=([\d.]+)s")
_RE_BATCH = re.compile(_TS + r".*Batch query released.*? total=([\d.]+)s")
_RE_PAGE = re.compile(_TS + r".*Realtime fund flow page .*?symbol=(\S+) .*?service=([\d.]+)s")
_RE_SKIPPED = re.compile(_TS + r".*Report cache skipped.*?symbol=(\S+) incomplete_sources=(\S+)")
_RE_VERSION = re.compile(_TS + r".*cn-stock-mcp version=(\S+)")
_RE_RENDER = re.compile(r"render=([0-9a-f]{12})")

#: 离散事件。key 是稳定的事件名（对外可见，别随便改），value 是匹配和一句人话。
EVENTS = {
    "impersonate_cooldown": (
        re.compile(_TS + r".*suspending impersonation"),
        "伪装通道暂停，期间东财源整体跳过"),
    "source_breaker_open": (
        re.compile(_TS + r".*Source breaker opened source=(\S+)"),
        "上游源熔断，改走备用源"),
    "caliber_change": (
        re.compile(_TS + r".*指数 (\d{6}) 的 K 线来自"),
        "指数 K 线落到备用源，成交量口径变了，报告里看不出来"),
    "tonghuashun_5xx": (
        re.compile(_TS + r".*同花顺取kline失败"),
        "同花顺年份文件取不到"),
    "session_crash": (
        re.compile(_TS + r".*ERROR Stateless session crashed"),
        "会话崩溃，多半是调用方超时断开"),
}

#: 阶段名 → 日志里的 Data task 名字。对外用中文，别把内部函数名漏给使用者。
STAGES = {
    "K 线取数": "_fetch_kline_sync",
    "资金流取数": "_fetch_fund_flow_cached",
    "实时行情": "_fetch_realtime_from_cache",
    "财务取数": "_fetch_finance_sync",
}


#: 判趋势时**每一半**要的最小样本量。低于它只报当前值，不报涨跌——十来个样本的
#: p95 是噪音，把噪音说成"性能下降"比不说更糟。
MIN_TREND_SAMPLES = 30
#: 基线 p90 低于这个数就不给百分比。多数是缓存命中的阶段 p90 本来就接近 0，
#: 除出来的 ±100% 没有意义。
_TREND_MIN_P90 = 0.5


def _mid_stamp(start: str, end: str) -> str:
    """两个时刻的中点，仍然是字符串（日志行按字符串比大小就够）。"""
    fmt = "%Y-%m-%d %H:%M:%S"
    a = dt.datetime.strptime(start, fmt)
    b = dt.datetime.strptime(end, fmt)
    return (a + (b - a) / 2).strftime(fmt)


@dataclass
class Series:
    """一组带时刻的耗时样本。平均和分位数都要——它们看的是两件事。

    平均被长尾拉高、分位数把长尾砍掉。只看 p50 会以为很健康，只看平均会以为普遍慢；
    两个并排才看得出"多数很快、少数极慢"这个形状。今天 K 线取数 avg 4.76s 而
    p50 只有 2.72s，差的那一倍就是 89 秒那条长尾——正好指向"该给它加个总预算"。
    """

    values: list = field(default_factory=list)     # [(时刻, 秒数)]

    def _stats(self, xs: list) -> Optional[dict]:
        if not xs:
            return None
        xs = sorted(xs)
        pick = lambda q: xs[min(len(xs) - 1, int(len(xs) * q))]   # noqa: E731
        return {"n": len(xs), "avg": sum(xs) / len(xs), "p50": pick(0.5),
                "p90": pick(0.9), "p95": pick(0.95), "max": xs[-1]}

    def stats(self) -> Optional[dict]:
        return self._stats([v for _, v in self.values])

    def trend(self, epoch_of=None) -> Optional[dict]:
        """**同一个纪元内**后半段 vs 前半段。

        必须限定在一个纪元里。踩过：一份 10:08→20:11 的日志跨了盘中、收盘、傍晚
        三个时段，直接对半分算出来 +98% ~ +787%，看着像性能崩了，其实只是换了运行
        时段——盘中资金流走缓存和页面，收盘后重新取数，两者本来就不是一回事。
        跨时段的基线不可比，比出来的涨跌是假的。

        按**时间中点**对半，两边都要过 MIN_TREND_SAMPLES 才给结论。

        按样本数对半是错的，也踩过：一份日志里 10:10–10:38 有一段密集采样、之后是
        稀疏的正常调用，对半分出来基线 26 分钟、当前 4 小时 46 分，跨度差 11 倍。
        那段密集调用缓存全热、天然快，于是算出 +251% 的"性能下降"——纯粹是采样
        密度的假象。性能变化要比的是同样长的两段时间。

        p90 太小时不给百分比：财务取数多数是缓存命中、p90 本来就是 0，除出来的
        ±100% 没有意义。
        """
        values, epoch = self.values, None
        if epoch_of is not None and values:
            # 取**最近的、样本够的**那个纪元。直接取最新样本所在的纪元不行：傍晚那一档
            # 常常只有零星几次调用，会把所有阶段的趋势一起判成"样本不足"，而盘中那一
            # 大段明明够。
            grouped: dict = {}
            for stamp, value in values:
                grouped.setdefault(epoch_of(stamp), []).append((stamp, value))
            usable = [(max(v)[0], k, v) for k, v in grouped.items()
                      if len(v) >= MIN_TREND_SAMPLES * 2]
            if not usable:
                return None
            _, epoch, values = max(usable)
        if len(values) < MIN_TREND_SAMPLES * 2:
            return None
        ordered = sorted(values)
        start, end = ordered[0][0], ordered[-1][0]
        midpoint = _mid_stamp(start, end)
        early = [v for t, v in ordered if t < midpoint]
        late = [v for t, v in ordered if t >= midpoint]
        if min(len(early), len(late)) < MIN_TREND_SAMPLES:
            return None
        before, after = self._stats(early), self._stats(late)
        result = {"baseline": before, "current": after, "epoch": epoch,
                  "from": start, "mid": midpoint, "to": end, "change_pct": None}
        if before["p90"] >= _TREND_MIN_P90:
            result["change_pct"] = (after["p90"] - before["p90"]) / before["p90"] * 100
        return result


def log_files(log_file: str, since: str) -> list:
    """按时间顺序返回要读的文件。当前那个天然就是本次启动至今。

    收的是**文件路径**不是目录：发布形态安装时包在 site-packages 里而日志写在仓库下，
    服务自己推不出来，由 start.sh 把 LOG_FILE 导进来（见 config.LOG_FILE）。
    """
    previous = log_file + ".bak"
    if since == "startup":
        return [log_file] if os.path.exists(log_file) else []
    return [p for p in (previous, log_file) if os.path.exists(p)]


def _read(path: str) -> tuple:
    """读尾部至多 MAX_BYTES。返回（文本, 是否截断）。"""
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        if size > MAX_BYTES:
            handle.seek(size - MAX_BYTES)
            handle.readline()          # 丢掉被切半的那行
            return handle.read(), True
        return handle.read(), False


def resolve_since(since: str, now: dt.datetime) -> Optional[dt.datetime]:
    """把 since 参数换成一个起始时刻。``startup`` 返回 None（整个当前文件都算）。"""
    since = (since or "startup").strip().lower()
    if since in ("startup", ""):
        return None
    if since == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if since == "epoch":
        from .market_session import phase_and_epoch          # 避免顶层循环导入
        # 纪元名形如 closed-2026-09-07 / live-2026-09-08，只取日期部分当下界；
        # 纪元内的精确起点由 market_session 决定，这里不重算，宁可多算一点。
        _, epoch = phase_and_epoch(now)
        day = epoch.rsplit("-", 3)[-3:]
        try:
            return dt.datetime.strptime("-".join(day), "%Y-%m-%d")
        except ValueError:
            return None
    match = re.fullmatch(r"(\d+)([mh])", since)
    if match:
        amount = int(match.group(1))
        delta = dt.timedelta(minutes=amount) if match.group(2) == "m" else dt.timedelta(hours=amount)
        return now - delta
    try:
        return dt.datetime.strptime(since, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def digest(log_file: str, *, since: str = "startup", symbol: str = "",
           now: Optional[dt.datetime] = None) -> dict:
    """扫日志，产出 health 要的全部结构化数据。"""
    now = now or dt.datetime.now()
    floor = resolve_since(since, now)
    wanted = {s.strip().upper() for s in symbol.split(",") if s.strip()}

    paths, truncated, text = log_files(log_file, since), False, []
    for path in paths:
        body, cut = _read(path)
        truncated = truncated or cut
        text.append(body)
    lines = "\n".join(text).splitlines()

    first_at = last_at = None
    restarts = 0
    version = fingerprint = ""
    symbols: list = []
    stages = defaultdict(Series)
    batches, pages = Series(), Series()
    missing_counter: Counter = Counter()
    missing_symbols = defaultdict(set)
    missing_last: dict = {}
    degraded_counter: Counter = Counter()
    inferred_sources: Counter = Counter()
    events = defaultdict(lambda: {"count": 0, "first": None, "last": None, "detail": "", "items": Counter()})

    for line in lines:
        stamp = line[:19]
        if len(stamp) != 19 or stamp[4] != "-":
            continue
        if floor is not None and stamp < floor.strftime("%Y-%m-%d %H:%M:%S"):
            continue
        first_at = first_at or stamp
        last_at = stamp

        if (m := _RE_VERSION.search(line)):
            restarts += 1
            version = m.group(2)
            continue
        if not fingerprint and (m := _RE_RENDER.search(line)):
            fingerprint = m.group(1)

        if (m := _RE_SYMBOL.search(line)):
            sym = m.group(4)
            if wanted and sym.upper() not in wanted:
                continue
            present = int(m.group(9)) if m.group(9) else None
            total = int(m.group(10)) if m.group(10) else None
            miss = [x for x in (m.group(11) or "").split(",") if x and x != "-"]
            degraded = [x for x in (m.group(12) or "").split(",") if x and x != "-"]
            symbols.append({"at": m.group(1), "tool": m.group(3), "symbol": sym,
                            "total": float(m.group(7)), "present": present,
                            "expected": total, "missing": miss})
            for name in miss:
                missing_counter[name] += 1
                missing_symbols[name].add(sym)
                missing_last[name] = m.group(1)
            for name in degraded:
                degraded_counter[name] += 1
            continue

        if (m := _RE_SKIPPED.search(line)):
            if not (wanted and m.group(2).upper() not in wanted):
                for source in m.group(3).split(","):
                    inferred_sources[source] += 1
            continue
        if (m := _RE_TASK.search(line)):
            if not (wanted and m.group(3).upper() not in wanted):
                stages[m.group(2)].values.append((m.group(1), float(m.group(4))))
            continue
        if (m := _RE_BATCH.search(line)):
            # 一批覆盖多个标的，按单个标的过滤时把它算进来是错的——那条耗时里
            # 有别的标的的份。过滤模式下整批这一行直接不收。
            if not wanted:
                batches.values.append((m.group(1), float(m.group(2))))
            continue
        if (m := _RE_PAGE.search(line)):
            # 这一行里的 symbol 是裸六位码（399001），没有市场前缀。
            if not (wanted and not any(w.endswith(m.group(2)) for w in wanted)):
                pages.values.append((m.group(1), float(m.group(3))))
            continue

        for kind, (pattern, detail) in EVENTS.items():
            if (m := pattern.search(line)):
                slot = events[kind]
                slot["count"] += 1
                slot["first"] = slot["first"] or m.group(1)
                slot["last"] = m.group(1)
                slot["detail"] = detail
                if m.lastindex and m.lastindex >= 2:
                    slot["items"][m.group(2)] += 1
                break

    return {
        "window": {"since": since, "from": first_at, "to": last_at,
                   "restarts": max(0, restarts - 1) if since != "startup" else 0,
                   "version": version, "fingerprint": fingerprint,
                   "files": [os.path.basename(p) for p in paths],
                   "path": log_file, "truncated": truncated},
        "symbols": symbols,
        "availability": _availability(symbols, inferred_sources),
        "missing": [{"dimension": name, "count": count,
                     "symbols": sorted(missing_symbols[name]),
                     "last_at": missing_last.get(name)}
                    for name, count in missing_counter.most_common()],
        "degraded": dict(degraded_counter),
        # 源级失败次数。有 present= 时它是旁证，没有时它是唯一的缺失线索。
        "failed_sources": dict(inferred_sources),
        "latency": _latency(batches, Series([(s["at"], s["total"]) for s in symbols]),
                            stages, pages),
        "events": {k: {kk: (dict(vv) if isinstance(vv, Counter) else vv)
                       for kk, vv in v.items()} for k, v in events.items()},
    }


def _epoch_of(stamp: str) -> str:
    """时刻 → 市场纪元名。趋势对比只在同一个纪元内做，见 Series.trend。"""
    from .market_session import phase_and_epoch

    try:
        _, epoch = phase_and_epoch(dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        return epoch
    except Exception:                                  # noqa: BLE001 - 解析不了就不分组
        return ""


def _latency(batches: Series, per_symbol: Series, stages: dict, pages: Series) -> dict:
    """每个阶段的分布 + 趋势。顺序是从外到内：整批 → 单标的 → 各取数阶段，
    这样一眼看得出瓶颈在哪一层。"""
    series = {"整批总计": batches, "单标的总计": per_symbol}
    series.update({label: stages[task] for label, task in STAGES.items()})
    series["资金流页面"] = pages
    return {name: {"stats": s.stats(), "trend": s.trend(_epoch_of)}
            for name, s in series.items()}


def _availability(symbols: list, inferred_sources: Counter) -> dict:
    """有 present= 就用实测的，没有就按上游失败反推，并把方法标出来。"""
    measured = [s for s in symbols if s["expected"]]
    if measured:
        expected_total = sum(s["expected"] for s in measured)
        present_total = sum(s["present"] for s in measured)
        return {"method": "measured", "expected": expected_total,
                "present": present_total, "missing": expected_total - present_total,
                "rate": present_total / expected_total if expected_total else None,
                "coverage": len(measured) / len(symbols) if symbols else 0.0}
    # 老日志：只能按源失败推。一个源失败带走它名下的全部维度。
    per_source = defaultdict(int)
    for tool_dims in CONTRACT.values():
        for dimension in tool_dims:
            per_source[dimension.source] += 1
    inferred_missing = sum(count * per_source.get(source, 1)
                           for source, count in inferred_sources.items())
    expected_total = sum(len(expected(s["tool"], s["symbol"])) for s in symbols)
    return {"method": "inferred", "expected": expected_total,
            "present": max(0, expected_total - inferred_missing),
            "missing": min(expected_total, inferred_missing),
            "rate": (max(0, expected_total - inferred_missing) / expected_total)
                    if expected_total else None,
            "coverage": 0.0}


__all__ = ["EVENTS", "MAX_BYTES", "STAGES", "Series", "digest", "log_files", "resolve_since"]
