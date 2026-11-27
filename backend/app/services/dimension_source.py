"""概念/行业板块的来源识别 —— 「这份板块分类是谁给的」的唯一出处。

概念板块与行业板块不是某个写死的数据源, 而是由 ``data/ext_data`` 下所有带
「概念」/「行业」字段的扩展表拼出来的 (匹配规则与 ``api/overview._dimension_field``
同源)。所以来源必须从扩展表自身的 ``pull.url`` 反查, 不能在页面上写死「同花顺」:
用户把扩展表换成自有源后, 标注要跟着变。

只做「域名 → 展示名」的静态映射, 不发网络请求, 也不读 parquet。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.market_overview_builder import _dimension_field as _detect_dimension_field

# 上游域名 → 展示名。与各插件 plugin.yaml 的 display_name、前端
# lib/source-labels.ts 的 HOST_LABELS 保持同一套命名。
_HOST_LABELS: tuple[tuple[str, str], ...] = (
    ("fuyao.aicubes.cn", "同花顺"),
    ("eastmoney.com", "东方财富"),
    ("gtimg.cn", "腾讯行情"),
    ("xtick.top", "XTick"),
    ("tickflow.org", "TickFlow"),
    ("shy313.com", "自维护种子源"),
)

DIMENSION_KINDS = ("concept", "industry")


def upstream_label(url: str | None) -> str | None:
    """拉取地址 → 上游展示名; 无法识别返回 None (不猜测)。"""
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for domain, label in _HOST_LABELS:
        if host == domain or host.endswith(f".{domain}"):
            return label
    return None


def dimension_field(config: ExtConfig, kind: str) -> str | None:
    """该扩展表在指定维度下可用的字段名 (概念/行业), 无则 None。"""
    if kind not in DIMENSION_KINDS:
        return None
    return _detect_dimension_field(config, kind)


def dimension_sources(data_dir: Path, kind: str) -> list[dict]:
    """当前概念/行业成分实际使用的扩展表与上游。

    返回 ``[{id, label, upstream}]``; ``upstream`` 无法识别时为 None,
    由调用方降级为"扩展数据"而不是冒充某个数据源。
    """
    if kind not in DIMENSION_KINDS:
        return []
    sources: list[dict] = []
    for config in ExtConfigStore(data_dir).load_all():
        if not dimension_field(config, kind):
            continue
        url = config.pull.url if config.pull is not None else None
        sources.append({
            "id": config.id,
            "label": config.label,
            "upstream": upstream_label(url),
        })
    return sources


def dimension_source_labels(data_dir: Path, kind: str) -> list[str]:
    """去重后的上游展示名清单 (供文案拼接); 无识别结果返回空列表。"""
    labels: list[str] = []
    for item in dimension_sources(data_dir, kind):
        upstream = item.get("upstream")
        if upstream and upstream not in labels:
            labels.append(upstream)
    return labels
