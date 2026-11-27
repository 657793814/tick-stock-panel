"""概念/行业板块的来源识别 — 域名反查与维度过滤。

不访问网络、不依赖真实 data/: 用 tmp_path 写入扩展表配置, 断言
「扩展表 pull.url → 上游展示名」的映射与「按概念/行业字段过滤」的口径。
来源标注必须跟着扩展表走, 不能在页面上写死同花顺。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.api import ext_data as ext_api
from app.services.dimension_source import (
    dimension_source_labels,
    dimension_sources,
    upstream_label,
)
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig


def _config(config_id: str, label: str, fields: list[str], url: str | None = None) -> ExtConfig:
    return ExtConfig(
        id=config_id,
        label=label,
        mode="snapshot",
        fields=[ExtField(name, "string", name) for name in fields],
        pull=PullConfig(url=url, enabled=False) if url else None,
    )


def _request(data_dir: Path) -> SimpleNamespace:
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_upstream_label_matches_host_not_path():
    assert upstream_label(
        "https://fuyao.aicubes.cn/api/a-share-index/catalog/ths-index-list?tag=cn_concept"
    ) == "同花顺"
    assert upstream_label("https://push2.eastmoney.com/api/qt/clist/get") == "东方财富"
    # 域名后缀冒充与非法地址一律返回 None, 不猜测来源
    assert upstream_label("https://fuyao.aicubes.cn.evil.com/x") is None
    assert upstream_label("not-a-url") is None
    assert upstream_label("") is None
    assert upstream_label(None) is None


def test_dimension_sources_filters_by_kind(tmp_path):
    store = ExtConfigStore(tmp_path)
    store.upsert(_config(
        "ext_gn_ths", "扩展概念", ["symbol", "所属概念"],
        "https://fuyao.aicubes.cn/api/a-share-index/catalog/ths-index-list?tag=cn_concept",
    ))
    store.upsert(_config(
        "ext_hy_ths", "扩展行业", ["symbol", "所属同花顺行业"],
        "https://fuyao.aicubes.cn/api/a-share-index/catalog/ths-index-list?tag=industry",
    ))
    store.upsert(_config("ext_fin", "财务扩展", ["symbol", "净利润"], "https://example.test/fin"))
    store.upsert(_config("ext_own", "自有题材", ["symbol", "custom_theme"], "https://data.mycorp.cn/x"))

    concept = dimension_sources(tmp_path, "concept")
    assert [item["id"] for item in concept] == ["ext_gn_ths", "ext_own"]
    assert concept[0]["upstream"] == "同花顺"
    # 自有源识别不出上游 → upstream 为 None, 由调用方降级, 不冒充同花顺
    assert concept[1]["upstream"] is None

    industry = dimension_sources(tmp_path, "industry")
    assert [item["id"] for item in industry] == ["ext_hy_ths"]
    assert dimension_source_labels(tmp_path, "concept") == ["同花顺"]
    # 非法维度 fail-closed, 不返回任何表
    assert dimension_sources(tmp_path, "index") == []


def test_ext_data_list_exposes_source_label(tmp_path):
    ExtConfigStore(tmp_path).upsert(_config(
        "ext_gn_ths", "扩展概念", ["symbol", "所属概念"],
        "https://fuyao.aicubes.cn/api/a-share-index/catalog/ths-index-list?tag=cn_concept",
    ))
    items = ext_api.list_configs(_request(tmp_path))["items"]
    assert [item["id"] for item in items] == ["ext_gn_ths"]
    assert items[0]["source_label"] == "同花顺"
