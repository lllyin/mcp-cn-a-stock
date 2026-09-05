"""各个上游平台的实现。

一个文件一个平台，文件里写清这个平台的全部事实：主机、请求头、编码、代码写法、
单位口径、已知的坑。加一个新平台就是加一个文件 + 在这里 import 一行。

import 的顺序无所谓——注册是幂等的，能力的启用顺序由各维度自己的 ``*_PROVIDERS``
环境变量决定，不由这里的顺序决定。
"""

from . import sina, tencent, tonghuashun, weekday  # noqa: F401  仅为触发注册

__all__ = ["sina", "tencent", "tonghuashun", "weekday"]
