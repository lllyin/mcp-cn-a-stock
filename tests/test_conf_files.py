"""参考数据文件（confs/*.json）的定位与兜底。

这一层没有测试的时候漏过一次最坏的故障：2026-09-06 装成包运行时，
``__file__/../confs`` 指到 site-packages（那里没有 confs），指数名单空掉，
``SH000001`` 被"纠正"成 ``SZ000001``——上证指数的报告里装的是平安银行的数据。
不报错、不降级、整份都是另一只证券。本地开发跑 ``python main.py``，包就在仓库
里，怎么跑都命中，所以照不出来。

所以这里测的两件事都针对"装成包"这个本地照不出的形态：
文件必须随包走，以及万一还是读不到，必须回落到内置名单而不是空集。
"""

import json
import logging

import pytest

from finmcp import config
from finmcp.datasource.cn_stock_source import CNStockDataSource


@pytest.fixture
def resolver():
    """不碰真实数据源，只借它的代码归一逻辑。"""
    return CNStockDataSource.__new__(CNStockDataSource)


class TestConfPath:
    def test_the_reference_data_ships_inside_the_package(self):
        """三个文件都必须能从包内定位到——装成包之后没有别的地方可找。"""
        for name in ("indices.json", "markets.json", "stock_sector.json"):
            path = config.conf_path(name)
            assert path.endswith(f"finmcp/confs/{name}"), path
            with open(path, encoding="utf-8") as handle:
                assert json.load(handle), f"{name} 是空的"

    def test_conf_dir_overrides_one_file_without_shadowing_the_rest(self, tmp_path, monkeypatch):
        """运维只换 indices.json 时，不该被迫把 markets.json 也复制过去。"""
        (tmp_path / "indices.json").write_text('{"sh_indices": ["000001"]}', encoding="utf-8")
        monkeypatch.setattr(config, "ENV_PREFIX", "")
        monkeypatch.setenv("CONF_DIR", str(tmp_path))

        assert config.conf_path("indices.json") == str(tmp_path / "indices.json")
        assert config.conf_path("markets.json").endswith("finmcp/confs/markets.json")

    def test_a_missing_override_falls_back_to_the_packaged_copy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "ENV_PREFIX", "")
        monkeypatch.setenv("CONF_DIR", str(tmp_path / "nowhere"))
        assert config.conf_path("indices.json").endswith("finmcp/confs/indices.json")


class TestIndicesFallback:
    """兜底名单曾经写在 ``except`` 里，而文件不存在不抛异常——所以它从不执行。"""

    def test_the_real_list_loads(self):
        sh, sz = config._load_indices(config.conf_path("indices.json"))
        assert {"000001", "000688"} <= sh
        assert {"399001", "399006"} <= sz

    @pytest.mark.parametrize(
        "content",
        [
            None,                                   # 文件根本不存在（装成包时的真实形态）
            "",                                     # 空文件
            "{ not json",                           # 坏 JSON
            '{"sh_indices": [], "sz_indices": []}',  # 名单为空：比缺文件更隐蔽
            '{"sz_indices": ["399001"]}',            # 只给了一半
        ],
        ids=["missing", "empty-file", "broken-json", "empty-lists", "half"],
    )
    def test_anything_unreadable_falls_back_instead_of_going_empty(
        self, tmp_path, caplog, content
    ):
        path = tmp_path / "indices.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="finmcp"):
            sh, sz = config._load_indices(str(path))

        assert sh == set(config._FALLBACK_SH_INDICES)
        assert sz == set(config._FALLBACK_SZ_INDICES)
        assert any("内置兜底名单" in r.getMessage() for r in caplog.records), \
            "回落必须留下日志，否则又是一次静默错数据"

    def test_the_loaded_list_is_never_empty_at_import_time(self):
        """真正要守的不变量：进程里这两个集合永远不为空。"""
        assert config.SH_INDICES
        assert config.SZ_INDICES
        assert config.ALL_INDICES == config.SH_INDICES | config.SZ_INDICES


class TestExchangeResolution:
    """名单空掉时的实际症状，直接钉住。"""

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("SH000001", ("000001", "sh")),   # 上证指数，不是平安银行
            ("SH000688", ("000688", "sh")),   # 科创50，不是国城矿业
            ("SZ399001", ("399001", "sz")),
            ("SZ399006", ("399006", "sz")),
            ("SZ000001", ("000001", "sz")),   # 真·平安银行
            ("SH600519", ("600519", "sh")),
            ("SH000333", ("000333", "sz")),   # 不在名单里的 SH000xxx 仍然纠正为深市
        ],
    )
    def test_index_symbols_keep_their_exchange(self, resolver, symbol, expected):
        assert resolver._symbol_to_akshare(symbol) == expected

    def test_an_empty_list_is_what_flipped_shanghai_to_shenzhen(self, resolver, monkeypatch):
        """复现故障本身：名单一空，沪市指数立刻变成深市个股。

        这条是反向锚：它保证上面那组不是碰巧过的，而是真的依赖名单。
        """
        monkeypatch.setattr(
            "finmcp.datasource.cn_stock_source.SH_INDICES", set()
        )
        assert resolver._symbol_to_akshare("SH000001") == ("000001", "sz")
