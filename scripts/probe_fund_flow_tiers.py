#!/usr/bin/env python3
"""探测 push2his ``fflow/daykline`` 按请求身份分档的行为。

背景：一次钉日期重放里，同一只标的的历史资金流拿到了与页面不一致的数值
（收盘价/涨跌幅一致，各单净额偏差 30%-50%），怀疑上游对某种客户端身份返回
一份扰动副本。curl 无论带什么 UA 只会落到「拒连」或「真值」两档，落不进
扰动档——分档判据大概率包含 TLS 指纹，而服务那条路是 Python requests。
所以本脚本不用 curl，全部用部署环境 venv 里的 requests 按服务的各种身份
各请求一次，与真值基准逐列比对，量出哪一档身份拿到什么。

用法（在部署机上、用服务的 venv；NID18 从浏览器或既有凭据取）：

    NID18=<nid18的值> python scripts/probe_fund_flow_tiers.py 1.600489
    NID18=... python scripts/probe_fund_flow_tiers.py 1.600489 \
        --dates 2026-07-01,2026-06-29 --repeat 3

产物：./fundflow_tier_probe_<时间戳>/ 下每个身份一份原始 JSON 响应，
stdout 一张对照表。把 stdout 和整个目录一起带回来即可。

各身份对应服务的哪条路（变体名见 VARIANTS）：
  - truth_impersonate   伪装通道（curl_cffi）+ 自洽 Chrome 头 + 凭据 → 预期真值基准
  - py_ua81_nocookie    原生 requests + akshare 硬编码的 Chrome/81 UA，无凭据
  - py_ua81_cookie      同上 + Cookie → 部署版「只补 Cookie」凭据层的行为
  - py_ua152_cookie     原生 requests + 自洽 Chrome/152 头 + Cookie → 修复后的形态
  - py_ua152_nocookie   自洽头但不带 Cookie
  - py_default          原生 requests 默认身份（Python UA），无凭据

另外每个变体都做两项内部一致性检查，用于判断扰动是「各桶独立扰动」还是
「整体自洽」：超大+大==主力；四档净额之和==0（接口原始值是精确的，页面
四舍五入后的值才有几百元以内的误差，容差按此设）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
PARAMS = {
    "lmt": "0",
    "klt": "101",
    "fields1": "f1,f2,f3,f7",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    "ut": "b2884a393a59ad64002292a3e90d46a5",
}
# fields2 按位置对应的列名，照 akshare 的列序。f64/f65 是保留位，丢弃。
RAW_FIELDS = (
    "日期",
    "主力净额", "小单净额", "中单净额", "大单净额", "超大单净额",
    "主力占比", "小单占比", "中单占比", "大单占比", "超大单占比",
    "收盘价", "涨跌幅",
)
AMOUNT_FIELDS = ("主力净额", "超大单净额", "大单净额", "中单净额", "小单净额")

_UA_81 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36")
_UA_152 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
# 与服务 http_channel._AUTH_HEADERS 同一套自洽头。
_HEADERS_152 = {
    "User-Agent": _UA_152,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://data.eastmoney.com/",
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
# 页面数据按两位小数（万）渲染后回归元，四档求和的舍入误差上界在千元的量级，
# 取 1 万元做容差：远高于舍入，远低于扰动档 30%-50% 的偏差。
_INVARIANT_TOLERANCE = 1e4


def _variant_headers(name: str, cookie: str) -> dict:
    headers = {"User-Agent": _UA_81} if "ua81" in name else dict(_HEADERS_152)
    if "default" in name:
        headers = {}
        return headers
    if cookie and "nocookie" not in name:
        headers["Cookie"] = f"nid18={cookie}"
    return headers


def _fetch_plain(name: str, cookie: str, outdir: Path, secid: str, timeout: float):
    headers = _variant_headers(name, cookie)
    try:
        resp = requests.get(URL, params={**PARAMS, "secid": secid},
                            headers=headers, timeout=timeout)
    except Exception as exc:  # 拒连本身是分档表的一格，不是探测失败
        return {"variant": name, "outcome": "REJECTED",
                "error": f"{type(exc).__name__}: {exc}"}
    path = outdir / f"{name}.json"
    path.write_bytes(resp.content)
    sent = {k: v for k, v in resp.request.headers.items()
            if k.lower() in ("user-agent", "cookie")}
    sent["cookie"] = "<present>" if sent.get("cookie") else "<absent>"
    return {"variant": name, "outcome": "HTTP_%s" % resp.status_code,
            "sent": sent, "body": resp.content, "path": path}


def _fetch_impersonate(name: str, cookie: str, outdir: Path, secid: str,
                       timeout: float):
    try:
        from curl_cffi import requests as imp_requests
    except ImportError:
        return {"variant": name, "outcome": "SKIPPED",
                "error": "curl_cffi 不可用，真值基准请改用 curl 手工获取"}
    headers = dict(_HEADERS_152)
    if cookie:
        headers["Cookie"] = f"nid18={cookie}"
    try:
        resp = imp_requests.get(URL, params={**PARAMS, "secid": secid},
                                headers=headers, impersonate="chrome",
                                timeout=timeout)
    except Exception as exc:
        return {"variant": name, "outcome": "REJECTED",
                "error": f"{type(exc).__name__}: {exc}"}
    path = outdir / f"{name}.json"
    path.write_bytes(resp.content)
    return {"variant": name, "outcome": "HTTP_%s" % resp.status_code,
            "sent": {"user-agent": _UA_152,
                     "cookie": "<present>" if cookie else "<absent>"},
            "body": resp.content, "path": path}


def _parse_klines(raw: bytes) -> dict:
    """响应 JSON 的 klines → {日期: {列名: 值}}。给不了数据就抛 ValueError。"""
    payload = json.loads(raw)
    klines = ((payload.get("data") or {}).get("klines")) or []
    if not klines:
        raise ValueError(f"data.klines 为空 (rc={payload.get('rc')})")
    rows = {}
    for line in klines:
        parts = line.split(",")
        rows[parts[0]] = dict(zip(RAW_FIELDS, (p.strip() for p in parts[:13])))
    return rows


def _f(row: dict, field: str):
    value = row.get(field)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _invariant_violations(row: dict) -> list:
    """内部一致性：超大+大==主力；四档金额之和==0；主力+中+小占比之和==0。

    金额在原始接口里是精确值；占比只有两位小数，每列 ±0.005 的舍入，三项求和
    的误差上界取 0.05。两套检查互为备份：万一扰动副本金额自洽而占比不自洽
    （或反过来），总有一套能咬住。
    """
    issues = []
    amounts = {name: _f(row, name) for name in AMOUNT_FIELDS}
    if all(v is not None for v in amounts.values()):
        major, xl, big, mid, small = (amounts[n] for n in AMOUNT_FIELDS)
        if abs(major - (xl + big)) > _INVARIANT_TOLERANCE:
            issues.append(f"主力({major:.0f}) != 超大+大({xl + big:.0f})，差 {major - xl - big:.0f} 元")
        total = xl + big + mid + small
        if abs(total) > _INVARIANT_TOLERANCE:
            issues.append(f"四档之和={total:.0f} 元，应为 0")
    ratios = {name: _f(row, name) for name in
              ("主力占比", "中单占比", "小单占比")}
    if all(v is not None for v in ratios.values()):
        total = ratios["主力占比"] + ratios["中单占比"] + ratios["小单占比"]
        if abs(total) > 0.05:
            issues.append(f"占比之和(主力+中+小)={total:.2f}%，应为 0")
    return issues


def _compare(row: dict, truth: dict) -> dict:
    """同一日期与基准逐列比：收盘价/涨跌幅 + 五个净额的相对差。"""
    report = {}
    for field in ("收盘价", "涨跌幅"):
        a, b = _f(row, field), _f(truth, field)
        report[field] = "一致" if a is not None and b is not None and abs(a - b) < 1e-9 \
            else f"{a} vs 基准 {b}"
    worst = 0.0
    for field in AMOUNT_FIELDS:
        a, b = _f(row, field), _f(truth, field)
        if a is None or b is None or b == 0:
            report[field] = f"{a} vs 基准 {b}"
            continue
        rel = abs(a - b) / abs(b)
        worst = max(worst, rel)
        report[field] = f"差 {a - b:+.0f} 元 ({rel:+.2%})" if rel > 5e-3 else "一致"
    report["_worst_rel"] = worst
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("secid", help="东财 secid，如 1.600489（沪）/ 0.000333（深）")
    parser.add_argument("--dates", default="2026-07-01,2026-06-29,2026-06-30",
                        help="比对的日期，逗号分隔")
    parser.add_argument("--repeat", type=int, default=1, help="整组重复次数（默认 1）")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--out", default=None, help="原始响应的保存目录")
    args = parser.parse_args()

    cookie = os.environ.get("NID18", "").strip()
    if not cookie:
        print("!! 未设 NID18 环境变量，带凭据的变体将按无 Cookie 发出，"
              "分档表会缺一角\n", file=sys.stderr)

    outdir = Path(args.out or ("fundflow_tier_probe_"
                               + datetime.now().strftime("%Y%m%d_%H%M%S")))
    outdir.mkdir(parents=True, exist_ok=True)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]

    variants = [
        ("truth_impersonate", "impersonate"),
        ("py_ua81_nocookie", "plain"),
        ("py_ua81_cookie", "plain"),
        ("py_ua152_cookie", "plain"),
        ("py_ua152_nocookie", "plain"),
        ("py_default_nocookie", "plain"),
    ]

    truth_rows: dict = {}
    summary: list = []
    for round_no in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"\n===== 第 {round_no}/{args.repeat} 轮 =====")
        for name, kind in variants:
            fetch = (_fetch_impersonate if kind == "impersonate" else _fetch_plain)
            result = fetch(name, cookie, outdir, args.secid, args.timeout)
            outcome = result["outcome"]
            line = f"[{name}] {outcome}"
            if result.get("error"):
                line += f"  {result['error']}"
                summary.append({"round": round_no, "variant": name,
                                "outcome": outcome, "error": result["error"]})
                print(line)
                time.sleep(2)
                continue
            try:
                rows = _parse_klines(result["body"])
            except ValueError as exc:
                print(line + f"  无数据：{exc}")
                summary.append({"round": round_no, "variant": name,
                                "outcome": outcome, "error": str(exc)})
                time.sleep(2)
                continue
            print(line + f"  rows={len(rows)} sent={result['sent']}")
            if name == "truth_impersonate":
                truth_rows = rows
                summary.append({"round": round_no, "variant": name,
                                "outcome": outcome, "rows": len(rows)})
            else:
                entry = {"round": round_no, "variant": name,
                         "outcome": outcome, "rows": len(rows), "dates": {}}
                for date in dates:
                    row = rows.get(date)
                    if row is None:
                        print(f"    {date}: 该档没有这一行")
                        entry["dates"][date] = "missing"
                        continue
                    issues = _invariant_violations(row)
                    if date in truth_rows:
                        cmp = _compare(row, truth_rows[date])
                        worst = cmp.pop("_worst_rel")
                        verdict = ("与基准一致" if worst < 5e-3
                                   else "!! 数值与基准不一致（疑似扰动副本）")
                        print(f"    {date}: {verdict}")
                        for field, detail in cmp.items():
                            print(f"      {field}: {detail}")
                        entry["dates"][date] = {"worst_rel": worst, "detail": cmp}
                    else:
                        print(f"    {date}: 无基准，仅做一致性检查")
                        entry["dates"][date] = {"no_truth": True}
                    if issues:
                        print(f"    {date}: 内部一致性不符 → " + "；".join(issues))
                        entry["dates"][date] = {**(entry["dates"][date]
                                                if isinstance(entry["dates"][date], dict)
                                                else {}),
                                                "invariant": issues}
                    else:
                        print(f"    {date}: 内部一致性 OK（超大+大==主力，四档和==0）")
                summary.append(entry)
            time.sleep(2)

    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始响应与 summary.json 已存到 {outdir}/ ，请把 stdout 和该目录一起带回。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
