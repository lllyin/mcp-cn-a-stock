#!/usr/bin/env python3
"""单核参考基准：把压测机的吞吐折算到部署机。

压测跑在开发机上，部署机是 2 核 4G 的 Ubuntu，核数和单核速度都不同，所以
压测出来的"次/分钟"不能直接搬。可搬的是压测报告里的 cpu_seconds_per_call：

    该机型吞吐上限(次/分钟) = 核数 x 60 / (cpu_seconds_per_call x 单核降级系数)

单核降级系数 = 部署机本脚本耗时 / 压测机本脚本耗时。在两台机器上分别跑

    python scripts/cpu_ref.py

用同一个解释器版本，取 total 一行相除即可。

三段负载对应服务里真实的 CPU 去处：解释器字节码、pandas 数值运算、JSON
序列化。只依赖 pandas 和标准库，不需要 psutil，Ubuntu 上直接可跑。
"""

from __future__ import annotations

import json
import platform
import sys
import time

import numpy as np
import pandas as pd


def bench(label: str, fn, repeat: int) -> tuple[str, float]:
    # 取最优值而非平均值：排除被其他进程抢占的样本，比较的是机器能力上限。
    best = min(_timed(fn) for _ in range(repeat))
    return label, best


def _timed(fn) -> float:
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


def interpreter_loop() -> None:
    total = 0
    for i in range(400_000):
        total += i * i % 7
    return total


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(20260903)
    return pd.DataFrame({
        "收盘": rng.uniform(5, 80, 4000),
        "成交量": rng.uniform(1e4, 1e7, 4000),
        "成交额": rng.uniform(1e6, 1e9, 4000),
    })


def pandas_work(frame: pd.DataFrame) -> None:
    # 与 K 线后处理同形：派生列、滚动窗口、分位数。
    frame = frame.copy()
    frame["涨跌幅"] = frame["收盘"].pct_change() * 100
    frame["均量"] = frame["成交量"].rolling(20).mean()
    frame["隐含股数"] = frame["成交额"] / frame["收盘"]
    frame["涨跌幅"].quantile([0.5, 0.9, 0.95])
    frame.dropna().to_dict("records")


def json_work(payload: list) -> None:
    json.loads(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    frame = _frame()
    payload = frame.head(600).to_dict("records")

    results = [
        bench("interpreter", interpreter_loop, 5),
        bench("pandas", lambda: pandas_work(frame), 5),
        bench("json", lambda: json_work(payload), 5),
    ]
    total = sum(value for _, value in results)

    print(f"python  : {platform.python_version()}")
    print(f"machine : {platform.system()} {platform.machine()}")
    for label, value in results:
        print(f"{label:<12}: {value * 1000:8.2f} ms")
    print(f"{'total':<12}: {total * 1000:8.2f} ms")
    print()
    print("降级系数 = 部署机 total / 压测机 total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
