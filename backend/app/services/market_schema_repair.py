"""修复 enriched 分区的列类型漂移（多市场扩展）。

症状: 港美股 enriched 写入时用裸 `pl.lit(0)` 生成连板数 → Int32, 而存储 schema
(app.parquet.ENRICHED_STORAGE_SCHEMA) 声明 UInt32。文件写下去之后, 所有按 schema
扫描的读方（scan_enriched_parquet）一读到港美股分区就报
`incoming: Int32 != target: UInt32` 并让整次扫描失败 —— A 股目录与港美股共表,
于是港股日K/均线/选股全部读不出来, 而且报错点在共用读路径上, 很难从页面看出来。

写入侧已改成显式 UInt32; 本模块处理已经落盘的存量分区: 按 schema 逐分区把
不匹配的列 cast 回声明类型, 再走仓库正常 merge-upsert 重写（原子替换 + 发布认领）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.parquet import ENRICHED_STORAGE_SCHEMA

logger = logging.getLogger(__name__)

TABLE = "kline_daily_enriched"


def repair_enriched_dtypes(repo, on_progress=None) -> dict:
    """逐日分区对齐 enriched 列类型。返回 {partitions, repaired, failed}。"""
    emit = on_progress or (lambda *a, **k: None)
    base = Path(repo.store.data_dir) / TABLE
    parts = sorted((p for p in base.glob("date=*") if p.is_dir()), key=lambda p: p.name)
    checked = repaired = failed = 0

    for idx, part in enumerate(parts, start=1):
        path = part / "part.parquet"
        if not path.exists():
            continue
        checked += 1
        try:
            df = pl.read_parquet(path)
            drifted = {
                col: dtype
                for col, dtype in ENRICHED_STORAGE_SCHEMA.items()
                if col in df.columns and df[col].dtype != dtype
            }
            if not drifted:
                continue
            # strict=False: 极少数历史行可能是 null 或越界值, 宁可留 null 也不中断整分区修复。
            fixed = df.with_columns([
                pl.col(col).cast(dtype, strict=False) for col, dtype in drifted.items()
            ])
            # 必须整分区直写, 不能走 append_enriched 的 merge-upsert: 那里用
            # pl.concat(diagonal_relaxed) 合并存量行, Int32(存量) + UInt32(修正值)
            # 会被提升成 Int64 —— 类型不但没修回来, 反而把整分区漂成第三种宽度。
            # 这里读到的是完整分区, 直接原子覆盖是等价且类型确定的写法。
            repo._atomic_write_parquet(fixed, path)  # noqa: SLF001
            repaired += 1
            logger.info("修复 enriched 分区 %s: %s", part.name, sorted(drifted))
        except Exception as e:  # noqa: BLE001 — 单分区失败继续, 结束时如实计数
            logger.warning("修复 enriched 分区失败 %s: %s", part.name, e)
            failed += 1
        if idx % 20 == 0:
            emit("repair_schema", min(95, int(100 * idx / max(len(parts), 1))),
                 f"已检查 {idx}/{len(parts)} 个分区", skip_log=True)

    emit("done", 100, f"分区类型修复完成: 检查 {checked}, 修复 {repaired}, 失败 {failed}")
    return {"partitions": checked, "repaired": repaired, "failed": failed}
