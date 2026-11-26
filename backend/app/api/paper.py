"""虚拟账户(模拟盘) API — 薄胶水: 校验/映射在 paper 域模块, 这里只做 HTTP 壳。

多账户: 所有端点接受 ?account=<id> (缺省 "default"); 帐户 ID 校验在域模块。

路由挂载在 app/main.py; 域模块自带进程内写锁, API 层不再加第二把锁
(镜像 lots API 的跨请求互斥语义, 但锁下沉到域)。
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app import cache_store
from app.market_time import cn_today, in_continuous_session
from app.markets import ALL_MARKETS, normalize_symbol
from app.strategy import paper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])

# B008: FastAPI 参数默认值需要 Query(...) 调用, 提为模块级单例复用。
_REPLAY_START = Query(None, description="成交日期起 (含)")
_REPLAY_END = Query(None, description="成交日期止 (含)")


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _acc(request: Request, account: str) -> str:
    try:
        return paper.validate_account_id(account)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class AccountModel(BaseModel):
    initial_cash: float
    account_id: str = paper.DEFAULT_ACCOUNT_ID
    name: str | None = None
    market: str = "cn"
    commission_pct: float = paper.DEFAULT_COMMISSION_PCT
    stamp_tax_pct: float | None = None  # 缺省按市场给默认（cn 沿用历史 0.001）
    slippage_bps: float = paper.DEFAULT_SLIPPAGE_BPS
    queue_limit_orders: bool = False


class OrderModel(BaseModel):
    symbol: str
    side: str                       # buy / sell
    qty: int | None = None
    amount: float | None = None
    order_type: str = "market"      # market / next_open / close
    ref_price: float | None = None  # amount 模式折算 & 买入资金预检


class SettingsModel(BaseModel):
    queue_limit_orders: bool | None = None
    commission_pct: float | None = None   # 佣金率 (0.00025 = 万2.5)
    stamp_tax_pct: float | None = None    # 印花税 (0.001 = 千1, 仅卖出)
    slippage_bps: float | None = None     # 滑点 (5 = 5bps)


def _resolve_asset_type(request: Request, symbol: str) -> str:
    repo = getattr(request.app.state, "repo", None)
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception:
        logging.getLogger(__name__).warning(
            "resolve_asset_type failed for %s, falling back to stock", symbol, exc_info=True
        )
        return "stock"


def _last_close(data_dir: Path, symbol: str, asset_type: str) -> float | None:
    """最近收盘价 (amount 折算参考价)。读最新分区日 raw close。"""
    base = paper._daily_path(data_dir, asset_type)
    if not base.exists():
        return None
    try:
        import polars as pl
        df = (
            pl.scan_parquet((base / "**" / "*.parquet").as_posix())
            .filter(pl.col("symbol") == symbol)
            .sort("date")
            .select(pl.col("close").last())
            .collect()
        )
    except Exception:
        return None
    if df.is_empty() or df["close"][0] is None:
        return None
    return float(df["close"][0])


def _name_map(request: Request, symbols: list[str]) -> dict[str, str]:
    """批量名称解析 (软失败: 维表不可用时回放仍可展示代码)。"""
    if not symbols:
        return {}
    repo = getattr(request.app.state, "repo", None)
    if repo is None or not hasattr(repo, "get_name_map"):
        return {}
    try:
        return repo.get_name_map(symbols)
    except Exception as e:  # noqa: BLE001 — 名称是展示增强, 不阻断回放
        logger.warning("paper replay name lookup failed: %s", e)
        return {}


@router.get("/accounts")
def list_accounts(request: Request):
    """账户列表 (带最新净值摘要, 供前端切换器)。"""
    return {"accounts": paper.list_accounts(_data_dir(request))}


@router.get("/account")
def get_account(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    acc = paper.get_account(_data_dir(request), _acc(request, account))
    return {"account": acc}


@router.post("/account")
def create_account(request: Request, body: AccountModel):
    if body.market not in ALL_MARKETS:
        raise HTTPException(status_code=400, detail=f"非法市场: {body.market}(可选 {ALL_MARKETS})")
    try:
        acc = paper.create_account(
            _data_dir(request),
            body.initial_cash,
            account_id=_acc(request, body.account_id),
            name=body.name,
            market=body.market,
            commission_pct=body.commission_pct,
            stamp_tax_pct=body.stamp_tax_pct,
            slippage_bps=body.slippage_bps,
            queue_limit_orders=body.queue_limit_orders,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


@router.delete("/account")
def delete_account(request: Request, force: bool = False, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """删除账户及其全部数据 (不可恢复)。仍有持仓或待成交订单时须 force=true。"""
    result, err = paper.delete_account(_data_dir(request), _acc(request, account), force=force)
    if err:
        raise HTTPException(status_code=400 if "force" in (err or "") else 404, detail=err)
    from app.strategy import paper_watch

    paper_watch.sync_from_state(request.app.state, _data_dir(request))  # 账户删了, 绑定集变了
    return {"ok": True, "deleted": _acc(request, account), **(result or {})}


@router.get("/overview")
def overview(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """总览: 现金 + 持仓估值 + 账户状态。

    盘中(交易时段且实时快照就绪)用当日实时 raw_close 估算 (estimating=true),
    其余时刻用最近日线收盘价 (与收盘定版口径一致)。
    """
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        return {"initialized": False, "account_id": acc_id}
    ov = paper.overview(data_dir, account_id=acc_id)
    ov["fees"] = {k: acc[k] for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")}

    # 盘中实时估算: 从行情服务的当日 enriched 快照取 raw_close (软失败, 降级日线)
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None and acc.get("status") == "active":
        try:
            enriched, enriched_date = qs.get_enriched_today()
            from app.market_time import cn_today
            if not enriched.is_empty() and enriched_date == cn_today() and ov.get("holdings"):
                snapshot = dict(zip(enriched["symbol"].to_list(), enriched["raw_close"].to_list(), strict=False))
                live = paper.overview(data_dir, snapshot, account_id=acc_id)
                live["fees"] = ov["fees"]
                return live
        except Exception as e:
            logger.warning("paper overview 实时估算失败, 降级日线口径: %s", e)
    return ov


@router.post("/orders")
def create_order(request: Request, body: OrderModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    asset_type = _resolve_asset_type(request, body.symbol)
    ref_price = body.ref_price
    if ref_price is None:
        ref_price = _last_close(data_dir, body.symbol, asset_type)
    order, err = paper.create_order(
        data_dir, body.symbol, body.side,
        account_id=acc_id,
        qty=body.qty, amount=body.amount,
        order_type=body.order_type, asset_type=asset_type, ref_price=ref_price,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    cache_store.invalidate_namespace("paper-live-feed")
    return {"order": order}


@router.get("/orders")
def list_orders(request: Request, status: str | None = None, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    orders = paper.load_orders(_data_dir(request), _acc(request, account))
    if status:
        orders = [o for o in orders if o["status"] == status]
    orders.sort(key=lambda o: o["created_at"], reverse=True)
    return {"orders": orders}


@router.get("/live-feed")
def live_feed(request: Request, limit: int = Query(40, ge=1, le=120)):
    """跨账户最近撮合流水 (只读), 供模拟盘资金全景。

    每个账户只读最近 8 个订单文件 (按 mtime); 结果按盘中 3s / 休市 30s
    共享缓存。额外附带标的名称与最新价/涨跌幅 (行情不可用时为空),
    让左栏「股票代号 → 名称 → 现价 → 涨跌幅」一次拿全。
    """
    key = str(limit)
    cached = cache_store.get_json("paper-live-feed", key)
    if cached is not None:
        return cached
    data_dir = _data_dir(request)
    items: list[dict] = []
    # 全量口径统计: 委托流水/活跃账户必须反映全部订单, 而不是 items 里那截
    # "最近流水" (每账户只读 8 单 + limit 截断, 数字会偏小且随轮询波动)。
    total_orders = total_buys = total_sells = 0
    active_accs: set[str] = set()
    for aid in paper.list_account_ids(data_dir):
        acc = paper.get_account(data_dir, aid) or {}
        name = str(acc.get("name") or aid)
        for o in paper.load_orders(data_dir, aid):
            total_orders += 1
            active_accs.add(aid)
            if o.get("side") == "buy":
                total_buys += 1
            elif o.get("side") == "sell":
                total_sells += 1
        for o in paper.recent_orders(data_dir, aid, limit=8):
            items.append({
                "id": o.get("id"),
                "account": aid,
                "name": name,
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "qty": o.get("qty"),
                "status": o.get("status"),
                "order_type": o.get("order_type"),
                "fill_price": o.get("fill_price"),
                "created_at": o.get("created_at"),
                "filled_at": o.get("filled_at"),
            })
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    items = items[:limit]

    # 标的名称 + 最新快照 (行情自检未完成/实时关闭时保持 None, 前端退回到成交价)
    symbols = sorted({str(it["symbol"]) for it in items if it.get("symbol")})
    names = _name_map(request, symbols)
    quote_map: dict[str, dict] = {}
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None and symbols:
        try:
            import polars as pl
            qdf = qs.get_quotes_compat()
            if not qdf.is_empty():
                cols = [c for c in ("symbol", "close", "change_pct") if c in qdf.columns]
                for row in qdf.filter(pl.col("symbol").is_in(symbols)).select(cols).to_dicts():
                    quote_map[str(row["symbol"])] = row
        except Exception as e:  # noqa: BLE001 — 行情是展示增强
            logger.debug("paper live-feed quotes unavailable: %s", e)
    # 实时快照缺失 (行情自检未完成等) 时退到日线: 最新收盘 + 对前一日涨跌幅。
    # 宁可显示「最近收盘」也不显示空值; 结果按标的集合缓存, 避免每次扫描 parquet。
    missing = [s for s in symbols if not (quote_map.get(s) or {}).get("close")]
    if missing:
        cache_key = ",".join(missing)
        cached_q = cache_store.get_json("paper-live-daily-quotes", cache_key)
        if cached_q is None:
            by_type: dict[str, list[str]] = {}
            for s in missing:
                by_type.setdefault(_resolve_asset_type(request, s), []).append(s)
            cached_q = paper.latest_closes(data_dir, by_type)
            cache_store.set_json("paper-live-daily-quotes", cache_key, cached_q, ttl=60)
        for s, row in (cached_q or {}).items():
            if s not in quote_map:
                quote_map[s] = row
    for it in items:
        symbol = str(it.get("symbol") or "")
        q = quote_map.get(symbol) or {}
        it["symbol_name"] = names.get(symbol) or ""
        it["last_price"] = q.get("close")
        it["change_pct"] = q.get("change_pct")

    result = {
        "items": items,
        "count": len(items),
        # 全量口径 (前端统计条用): items 只是最近流水的展示窗口
        "totals": {
            "orders": total_orders,
            "buys": total_buys,
            "sells": total_sells,
            "active_accounts": len(active_accs),
        },
    }
    cache_store.set_json(
        "paper-live-feed", key, result,
        ttl=3 if in_continuous_session() else 30,
    )
    return result


@router.delete("/orders/{order_id}")
def cancel_order(request: Request, order_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    order, err = paper.cancel_order(_data_dir(request), order_id, _acc(request, account))
    if err:
        raise HTTPException(status_code=400, detail=err)
    cache_store.invalidate_namespace("paper-live-feed")
    return {"order": order}


@router.get("/trades")
def list_trades(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    fills = paper.load_fills(_data_dir(request), _acc(request, account))
    fills.sort(key=lambda f: f.get("seq", 0), reverse=True)
    return {"fills": fills}


@router.get("/replay/symbols")
def replay_symbols(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """账户成交过的标的摘要 (回放标的列表)。"""
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    if paper.get_account(data_dir, acc_id) is None:
        raise HTTPException(status_code=404, detail="账户不存在")
    symbols = sorted({f["symbol"] for f in paper.load_fills(data_dir, acc_id) if f.get("symbol")})
    return {"symbols": paper.replay_symbols(data_dir, acc_id, names=_name_map(request, symbols))}


@router.get("/replay")
def replay(
    request: Request,
    symbol: str = Query(..., description="标的代码, 支持 600519 / hk00700 / AAPL 等写法"),
    start: date | None = _REPLAY_START,
    end: date | None = _REPLAY_END,
    limit: int = Query(500, ge=1, le=2000, description="最多返回成交笔数"),
    account: str = Query(paper.DEFAULT_ACCOUNT_ID),
):
    """单标的成交回放: 成交明细 + K 线标记 + 区间统计。"""
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="start 不能晚于 end")
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    if paper.get_account(data_dir, acc_id) is None:
        raise HTTPException(status_code=404, detail="账户不存在")
    sym = normalize_symbol(symbol)
    data = paper.replay(
        data_dir,
        sym,
        acc_id,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
        limit=limit,
        names=_name_map(request, [sym]),
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"该账户无 {sym} 成交")
    return data


@router.get("/positions")
def list_positions(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    data_dir = _data_dir(request)
    ov = paper.overview(data_dir, account_id=_acc(request, account))
    return {"holdings": ov.get("holdings", []), "initialized": ov.get("initialized", False)}


@router.get("/nav")
def list_nav(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return {"nav": paper.load_nav(_data_dir(request), _acc(request, account))}


@router.get("/stats")
def get_stats(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    return paper.stats(_data_dir(request), _acc(request, account))


@router.get("/rounds")
def list_rounds(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """FIFO 配对回合明细 (开平日期/数量/盈亏/持有天数), 供账户面板流水展示。"""
    return {"rounds": paper.round_trips(_data_dir(request), _acc(request, account))}


@router.get("/compare")
def compare_accounts(request: Request):
    """横向对比全部账户: 概览 + 回合统计 + 定版净值 (供对比表与净值叠加图)。

    净值给全量定版序列, 前端做归一化 (起点=1) 后多账户叠加。
    对比增量字段 (holdings_count/created_at/last_nav_date/day_change_pct/auto_rules)
    为加法扩展, 旧前端忽略不影响。
    """
    from app.strategy import paper_auto

    data_dir = _data_dir(request)

    # 盘中实时估算: 当日 enriched 快照就绪时取一次 raw_close 映射, 之后每账户
    # 纯内存 overview (与 /overview 实时口径一致); 失败降级定版口径
    live_snapshot: dict[str, float] | None = None
    qs = getattr(request.app.state, "quote_service", None)
    if qs is not None:
        try:
            enriched, enriched_date = qs.get_enriched_today()
            from app.market_time import cn_today
            if not enriched.is_empty() and enriched_date == cn_today():
                live_snapshot = dict(
                    zip(enriched["symbol"].to_list(), enriched["raw_close"].to_list(), strict=False)
                )
        except Exception as e:  # noqa: BLE001 — 快照来源不定 (行情服务/数据缺失), 软失败降级定版口径
            logger.warning("paper compare 实时快照不可用, 用定版口径: %s", e)

    rows = []
    account_ids = paper.list_account_ids(data_dir)
    # 定版口径: 多账户一次性取价, 避免逐账户扫盘 (账户多时对比接口会拖到几十秒)
    mark_day = paper.latest_data_day(data_dir)
    shared_prices = None
    if live_snapshot is None:
        shared_prices = paper.prices_for_accounts(data_dir, account_ids, mark_day)
    for acc_id in account_ids:
        acc = paper.get_account(data_dir, acc_id)
        if acc is None:
            continue
        ov = paper.overview(
            data_dir,
            shared_prices,
            account_id=acc_id,
            estimating=live_snapshot is not None,
            price_date=None if live_snapshot is not None else mark_day,
        )
        if not ov.get("initialized"):
            continue  # 空壳目录 (懒创建) 不进对比
        st = paper.stats(data_dir, acc_id)
        initial = float(acc.get("initial_cash") or 0)
        nav_rows = paper.load_nav(data_dir, acc_id)
        day_change_pct = None
        if len(nav_rows) >= 2 and float(nav_rows[-2]["nav"] or 0) > 0:
            day_change_pct = round((float(nav_rows[-1]["nav"]) / float(nav_rows[-2]["nav"]) - 1) * 100, 2)
        rules = paper_auto.load_auto_rules(data_dir, acc_id)
        # 盘中估算字段 (加法扩展): live_* 为快照价口径, 旧前端忽略不影响
        live_total = live_pnl_pct = None
        if live_snapshot is not None and acc.get("status") == "active":
            live_ov = paper.overview(data_dir, live_snapshot, account_id=acc_id)
            if live_ov.get("initialized"):
                live_total = live_ov.get("total")
                if initial > 0:
                    live_pnl_pct = round((live_ov.get("total_pnl") or 0) / initial * 100, 2)
        rows.append({
            "live_total": live_total,
            "live_pnl_pct": live_pnl_pct,
            "estimating": live_total is not None,
            "account": acc_id,
            "name": acc.get("name") or acc_id,
            "market": acc.get("market", "cn"),
            "status": acc.get("status"),
            "initial_cash": initial,
            "fees": {k: acc.get(k) for k in ("commission_pct", "stamp_tax_pct", "slippage_bps")},
            "total": ov.get("total"),
            "cash": ov.get("cash"),
            "market_value": ov.get("market_value"),
            "total_pnl": ov.get("total_pnl"),
            "pnl_pct": round((ov.get("total_pnl") or 0) / initial * 100, 2) if initial > 0 else None,
            "rounds": st.get("rounds"),
            "win_rate": st.get("win_rate"),
            "profit_loss_ratio": st.get("profit_loss_ratio"),
            "avg_holding_days": st.get("avg_holding_days"),
            "realized_pnl": st.get("realized_pnl"),
            "max_drawdown": st.get("max_drawdown"),
            "nav": [{"date": n["date"], "nav": n["nav"]} for n in nav_rows],
            # 对比展示用增量字段
            "holdings_count": len(ov.get("holdings") or []),
            "created_at": acc.get("created_at"),
            "last_nav_date": nav_rows[-1]["date"] if nav_rows else None,
            "day_change_pct": day_change_pct,
            "auto_rules": [
                {
                    "name": r.get("name"),
                    "match_kind": r.get("match_kind"),
                    "match_id": r.get("match_id"),
                    "side": r.get("side"),
                    "enabled": bool(r.get("enabled")),
                }
                for r in rules
            ],
            "auto_enabled": sum(1 for r in rules if r.get("enabled")),
        })
    return {"accounts": rows}


@router.post("/rebuild")
def rebuild(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """由成交台账重建物化持仓与现金 (修复兜底)。"""
    positions = paper.rebuild_positions(_data_dir(request), _acc(request, account))
    return {"symbols": len(positions)}


@router.post("/settle")
def settle(
    request: Request,
    day: str | None = Query(None, description="结算到该日 (缺省今天), 缺省时会自动补此前漏结算的交易日"),
    account: str | None = Query(None, description="账户 id; 缺省结算全部账户"),
):
    """手动/补结算: 撮合顺延单 → 除权调整 → 定版净值。

    盘后管道只在当天结算一次, 进程停机或管道在日K阶段失败就会漏掉, 顺延单
    (next_open/close) 便一直挂着「待成交」。此端点按天回放 (幂等, 不会重复成交),
    用于手动补账; 定时路径已在管道内自动补结算。
    """
    data_dir = _data_dir(request)
    target = day or cn_today().isoformat()
    try:
        date.fromisoformat(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"日期格式应为 YYYY-MM-DD: {target}") from e
    acc_ids = [_acc(request, account)] if account else paper.list_account_ids(data_dir)
    results = [paper.settle_pending_days(data_dir, target, account_id=aid) for aid in acc_ids]
    return {
        "day": target,
        "accounts": results,
        "filled": sum(r.get("filled", 0) for r in results),
        "expired": sum(r.get("expired", 0) for r in results),
    }


@router.post("/freeze")
def freeze(request: Request, frozen: bool = True, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """冻结/解冻账户: 冻结后拒绝新订单, 持仓只读。"""
    data_dir = _data_dir(request)
    acc_id = _acc(request, account)
    acc = paper.get_account(data_dir, acc_id)
    if acc is None:
        raise HTTPException(status_code=400, detail="尚未创建模拟账户")
    acc["status"] = "frozen" if frozen else "active"
    paper.save_account(data_dir, acc, acc_id)
    return {"account": acc}


@router.post("/settings")
def update_settings(request: Request, body: SettingsModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    """更新账户设置 (涨跌停排队等开关)。"""
    try:
        acc = paper.update_settings(_data_dir(request), _acc(request, account), **body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": acc}


# ===== 自动跟单规则 (V2) =====
class AutoRuleModel(BaseModel):
    name: str
    match_kind: str                 # strategy / rule
    match_id: str
    side: str = "buy"
    size_mode: str = "fixed_amount" # fixed_amount / pct_equity
    size_value: float
    order_type: str = "next_open"
    cooldown_days: int = 5
    max_positions: int = 0             # 最大同时持仓标的数, 0 不限 (对齐回测口径)
    enabled: bool = True


@router.get("/auto_rules")
def list_auto_rules(request: Request, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto
    return {"rules": paper_auto.load_auto_rules(_data_dir(request), _acc(request, account))}


@router.post("/auto_rules")
def create_auto_rule(request: Request, body: AutoRuleModel, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto, paper_watch
    try:
        rule = paper_auto.create_auto_rule(_data_dir(request), body.model_dump(), _acc(request, account))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    paper_watch.sync_from_state(request.app.state, _data_dir(request))  # 绑定即评估
    return {"rule": rule}


@router.post("/auto_rules/{rule_id}/enabled")
def set_auto_rule_enabled(request: Request, rule_id: str, enabled: bool, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto, paper_watch
    rule = paper_auto.set_enabled(_data_dir(request), rule_id, enabled, _acc(request, account))
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    paper_watch.sync_from_state(request.app.state, _data_dir(request))
    return {"rule": rule}


@router.delete("/auto_rules/{rule_id}")
def delete_auto_rule(request: Request, rule_id: str, account: str = Query(paper.DEFAULT_ACCOUNT_ID)):
    from app.strategy import paper_auto, paper_watch
    if not paper_auto.delete_auto_rule(_data_dir(request), rule_id, _acc(request, account)):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    paper_watch.sync_from_state(request.app.state, _data_dir(request))
    return {"ok": True}


# ===== 对比批量创建 (V3): 同规格多账户并行对比的构造入口 =====
class ArenaSourceModel(BaseModel):
    name: str | None = None              # 账户名 (缺省用 match_id)
    match_kind: str                      # strategy / rule (与 auto_rules 同口径)
    match_id: str
    side: str = "buy"
    size_mode: str = "pct_equity"        # 对比默认按权益百分比, 不同本金也可公平比
    size_value: float = 10.0
    order_type: str = "next_open"
    cooldown_days: int = 5
    max_positions: int = 0             # 最大同时持仓标的数, 0 不限 (对齐回测口径)


class ArenaBatchModel(BaseModel):
    initial_cash: float
    sources: list[ArenaSourceModel]
    name_prefix: str = ""                # 账户名前缀 (如 "第一期-", 可空)
    market: str = "cn"                   # 与市场语义对齐；旧请求默认 A 股
    commission_pct: float = paper.DEFAULT_COMMISSION_PCT
    stamp_tax_pct: float | None = None   # 缺省按市场给默认（cn 沿用历史 0.001）
    slippage_bps: float = paper.DEFAULT_SLIPPAGE_BPS
    queue_limit_orders: bool = False


@router.post("/arena/batch_create")
def arena_batch_create(request: Request, body: ArenaBatchModel):
    """对比批量创建: 同本金/同费率一次性开 N 个账户, 各绑一条自动跟单规则。

    公平对比由构造保证 (同初始资金 + 同费用口径 + 同成交约束, 复用 paper 域
    的锁与校验)。先整体校验全部规则再落盘 — 任一来源非法即 400, 不留半创建
    状态; 账户 id 服务端生成 (arena_ + 随机后缀), 不接受调用方指定。
    """
    from app.strategy import paper_auto

    if not body.sources:
        raise HTTPException(status_code=400, detail="至少需要一个来源")
    if len(body.sources) > 60:
        raise HTTPException(status_code=400, detail="单次最多创建 60 个对比账户")
    if body.market not in ALL_MARKETS:
        raise HTTPException(status_code=400, detail=f"非法市场: {body.market}(可选 {ALL_MARKETS})")

    data_dir = _data_dir(request)
    try:
        prepared = []
        for src in body.sources:
            rule = {
                "name": f"对比·{(src.name or '').strip() or src.match_id}",
                "match_kind": src.match_kind,
                "match_id": (src.match_id or "").strip(),
                "side": src.side,
                "size_mode": src.size_mode,
                "size_value": src.size_value,
                "order_type": src.order_type,
                "cooldown_days": src.cooldown_days,
                "max_positions": src.max_positions,
                "enabled": True,
            }
            paper_auto.validate_rule(rule)  # 全部通过前不落盘 (fail-closed)
            suffix = (src.name or "").strip() or src.match_id
            prefix = (body.name_prefix or "").strip()
            acc_name = f"{prefix}{suffix}" if prefix else suffix
            prepared.append((rule, acc_name))

        created = []
        for rule, acc_name in prepared:
            account_id = f"arena_{uuid.uuid4().hex[:10]}"
            paper.create_account(
                data_dir, body.initial_cash,
                account_id=paper.validate_account_id(account_id),
                name=acc_name,
                market=body.market,
                commission_pct=body.commission_pct,
                stamp_tax_pct=body.stamp_tax_pct,
                slippage_bps=body.slippage_bps,
                queue_limit_orders=body.queue_limit_orders,
            )
            saved = paper_auto.create_auto_rule(data_dir, rule, account_id=account_id)
            created.append({"account": account_id, "name": acc_name, "rule_id": saved["id"]})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    from app.strategy import paper_watch

    paper_watch.sync_from_state(request.app.state, data_dir)  # 绑定即评估
    return {"created": created}
