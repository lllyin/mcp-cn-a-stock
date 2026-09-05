# 开发与维护

这份文档给要改这个项目的人看。只想跑起来用的话，README 就够了。

## 环境

```bash
uv sync --extra dev
```

## 运行测试

```bash
pytest tests --ignore=tests/test_akshare_source.py
```

`test_akshare_source.py` 会真的打上游接口，默认跳过；要跑它得能连到东财。

## 调试

MCP Inspector 可以直接点着调用每个工具：

```bash
npx @modelcontextprotocol/inspector --url http://localhost:8686/cnstock/mcp
```

查看版本：

```bash
python -c "from finmcp import __version__; print(__version__)"
```

## 改数据源之前先读

- [取数架构：平台 → 能力 → 归一](data-provider-architecture.md)——接一个新数据源应该只需要
  写一个类加一行注册。如果发现要改别的文件，那是抽象出了问题。
- [技术实现说明](technical-details.md)——数据链路、回退、输出契约、报告缓存的取值依据。
- 项目约束见仓库根目录的 `AGENTS.md`，改动前请先读，尤其是「数据完整 > 功能正确 > 性能」
  和「改动前先算收益与副作用」两条。

## 发布前的验证

```bash
export MCPORTER_CONFIG=~/.openclaw/workspace/config/mcporter.json
python scripts/verify_release.py
```

它会实调每个工具、按维度契约逐项检查、把结果和 `verification/baseline/` 里的历史归档
重放比对，最后给一个可用率。≥99% 才发。

重构取数层时还要证明行为没变：

```bash
# 改之前
python scripts/prove_equivalence.py capture before
# 改完之后
python scripts/prove_equivalence.py capture after
python scripts/prove_equivalence.py diff before after
```

比对前记得把 `REPORT_CACHE_ENABLED` 设成 0——不然「改完」那次读的是「改之前」写的
字节，等价性是假的（脚本会检查，没关会拒绝跑）。
