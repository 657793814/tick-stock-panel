"""模拟盘成交回放读模型测试 — 台账只读、区间统计、标记与账户隔离。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router
from app.strategy import paper


def _account(tmp_path: Path, account_id: str = "default") -> None:
    paper.create_account(tmp_path, 1_000_000, account_id=account_id)


def _fill(
    tmp_path: Path,
    day: str,
    side: str,
    qty: int,
    price: float,
    *,
    account_id: str = "default",
    symbol: str = "600519.SH",
    fee: float = 5.0,
    order_id: str | None = None,
    seq: int | None = None,
) -> None:
    paper._append_fill(
        tmp_path,
        {
            "seq": seq if seq is not None else int(day.replace("-", "")) * 1000 + qty,
            "ts": f"{day}T15:00:00+08:00",
            "date": day,
            "order_id": order_id,
            "symbol": symbol,
            "asset_type": "stock",
            "side": side,
            "qty": qty,
            "price": price,
            "fee": fee,
            "kind": "fill",
        },
        account_id,
    )


def _corp_action(tmp_path: Path, day: str, factor: float = 2.0) -> None:
    paper._append_fill(
        tmp_path,
        {
            "seq": int(day.replace("-", "")) * 1000 + 999,
            "ts": f"{day}T15:00:00+08:00",
            "date": day,
            "order_id": None,
            "symbol": "600519.SH",
            "asset_type": "stock",
            "side": "corp_action",
            "kind": "corp_action",
            "factor": factor,
            "qty_before": 200,
            "cost_before": 10.5,
        },
    )


def _seed_round_trip(tmp_path: Path) -> None:
    _account(tmp_path)
    _fill(tmp_path, "2026-01-02", "buy", 100, 10.0, order_id="order_buy_1")
    _fill(tmp_path, "2026-01-05", "sell", 100, 12.0, order_id="order_sell_1")
    _fill(tmp_path, "2026-01-06", "buy", 200, 11.0, order_id="order_buy_2")
    _corp_action(tmp_path, "2026-01-07")


def test_replay_symbols_groups_and_isolates_accounts(tmp_path: Path):
    _seed_round_trip(tmp_path)
    _fill(tmp_path, "2026-01-03", "buy", 100, 400.0, symbol="00700.HK")
    _account(tmp_path, "other")
    _fill(tmp_path, "2026-01-03", "buy", 1, 180.0, account_id="other", symbol="AAPL.US")

    rows = paper.replay_symbols(tmp_path, names={"600519.SH": "贵州茅台"})
    assert [r["symbol"] for r in rows] == ["600519.SH", "00700.HK"]
    cn = rows[0]
    assert cn["name"] == "贵州茅台"
    assert cn["market"] == "cn"
    assert (cn["fills"], cn["buys"], cn["sells"]) == (3, 2, 1)
    assert (cn["first_date"], cn["last_date"], cn["last_side"]) == ("2026-01-02", "2026-01-06", "buy")
    assert cn["realized_pnl"] == 190.0
    assert rows[1]["market"] == "hk"

    # 另一个账户的成交不能串进来
    assert all(r["symbol"] != "AAPL.US" for r in rows)


def test_replay_marks_fills_and_summary(tmp_path: Path):
    _seed_round_trip(tmp_path)
    data = paper.replay(tmp_path, "600519.SH")
    assert data is not None
    assert data["market"] == "cn"
    assert len(data["fills"]) == 3
    assert len(data["markers"]) == 4
    assert [m["kind"] for m in data["markers"]] == ["buy", "sell", "buy", "neutral"]
    assert [m["label"] for m in data["markers"]] == ["B", "S", "B", "权"]
    assert data["markers"][1]["above"] is True
    assert data["summary"] == {
        "buys": 2,
        "sells": 1,
        "buy_amount": 3200.0,
        "sell_amount": 1200.0,
        "fees": 15.0,
        "realized_pnl": 190.0,
        "win_rate": 100.0,
        "avg_holding_days": 3.0,
        "open_qty": 400,
        "rounds": 1,
        "corp_actions": 1,
    }
    assert data["truncated"] is False


def test_replay_date_range_and_limit(tmp_path: Path):
    _seed_round_trip(tmp_path)
    day = paper.replay(tmp_path, "600519", start="2026-01-05", end="2026-01-05")
    assert day is not None
    assert [f["side"] for f in day["fills"]] == ["sell"]
    assert day["summary"]["realized_pnl"] == 190.0
    assert day["summary"]["open_qty"] == 0
    assert day["summary"]["corp_actions"] == 0

    limited = paper.replay(tmp_path, "600519.SH", limit=1)
    assert limited is not None
    assert limited["truncated"] is True
    assert limited["total_fills"] == 3
    assert len(limited["fills"]) == 1
    assert limited["fills"][0]["date"] == "2026-01-06"
    # 汇总仍按完整区间, 不受截断影响
    assert limited["summary"]["realized_pnl"] == 190.0


def test_replay_reason_from_order_and_execution_event(tmp_path: Path):
    _account(tmp_path)
    paper.save_order(
        tmp_path,
        {
            "id": "order_buy_1",
            "symbol": "600519.SH",
            "side": "buy",
            "qty": 100,
            "status": "filled",
            "source": "strategy-exec:token123:signal",
            "created_at": "2026-01-02T15:00:00+08:00",
        },
    )
    _fill(tmp_path, "2026-01-02", "buy", 100, 10.0, order_id="order_buy_1")

    data = paper.replay(tmp_path, "600519.SH")
    assert data is not None
    assert data["fills"][0]["reason"] == "signal"
    assert data["fills"][0]["reason_label"] == "策略买入"
    assert data["markers"][0]["reason_label"] == "策略买入"


def test_replay_unknown_symbol_returns_none(tmp_path: Path):
    _account(tmp_path)
    assert paper.replay(tmp_path, "600519.SH") is None


def test_replay_loads_strategy_meta(tmp_path: Path):
    _account(tmp_path)
    root = tmp_path / "user_data" / "extensions" / "paper_execution"
    root.mkdir(parents=True)
    (root / "default.json").write_text(
        json.dumps({"version": 1, "account": "default", "enabled": True, "status": "running",
                    "snapshot": {"strategy_id": "s1", "name": "均线突破", "source": "回测配置"}}),
        encoding="utf-8",
    )
    assert paper.load_execution_meta(tmp_path) == {
        "strategy_id": "s1",
        "name": "均线突破",
        "source": "回测配置",
        "enabled": True,
        "status": "running",
        "bound_at": None,
    }


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = type("Repo", (), {
        "store": type("Store", (), {"data_dir": tmp_path})(),
        "resolve_asset_type": staticmethod(lambda s: "stock"),
        "get_name_map": staticmethod(lambda symbols: {s: "贵州茅台" for s in symbols if s == "600519.SH"}),
    })()
    return TestClient(app)


def test_replay_api_contract(tmp_path: Path):
    _seed_round_trip(tmp_path)
    client = _client(tmp_path)

    r = client.get("/api/paper/replay/symbols")
    assert r.status_code == 200
    assert r.json()["symbols"][0]["symbol"] == "600519.SH"

    r = client.get("/api/paper/replay", params={"symbol": "600519"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "600519.SH"
    assert body["name"] == "贵州茅台"
    assert len(body["markers"]) == 4

    assert client.get("/api/paper/replay", params={"symbol": "600519.SH", "start": "2026-02-01", "end": "2026-01-01"}).status_code == 400
    assert client.get("/api/paper/replay", params={"symbol": "000001.SZ"}).status_code == 404
    assert client.get("/api/paper/replay", params={"symbol": "600519.SH", "account": "missing"}).status_code == 404

def test_replay_round_builds_open_and_close_markers(tmp_path: Path):
    _seed_round_trip(tmp_path)
    data = paper.replay_round(tmp_path, "600519.SH", "2026-01-02", "2026-01-05")
    assert data is not None
    assert data["round"]["open_date"] == "2026-01-02"
    assert data["round"]["close_date"] == "2026-01-05"
    assert data["round"]["qty"] == 100
    assert len(data["markers"]) == 2
    assert [m["kind"] for m in data["markers"]] == ["buy", "sell"]
    assert [m["label"] for m in data["markers"]] == ["B", "S"]
    assert data["markers"][0]["above"] is False
    assert data["markers"][1]["above"] is True
    assert data["markers"][0]["price"] == 10.0
    assert data["markers"][1]["price"] == 12.0
    assert data["round_summary"]["open_price"] == 10.0
    assert data["round_summary"]["close_price"] == 12.0


def test_replay_round_no_match_returns_none(tmp_path: Path):
    _seed_round_trip(tmp_path)
    assert paper.replay_round(tmp_path, "600519.SH", "2026-01-02", "2026-01-06") is None
    assert paper.replay_round(tmp_path, "600519.SH", "2026-01-06", "2026-01-07") is None


def test_replay_round_api_contract(tmp_path: Path):
    _seed_round_trip(tmp_path)
    client = _client(tmp_path)

    r = client.get("/api/paper/replay/round", params={"symbol": "600519", "open_date": "2026-01-02", "close_date": "2026-01-05"})
    assert r.status_code == 200
    body = r.json()
    assert body["markers"][0]["kind"] == "buy"
    assert body["markers"][1]["kind"] == "sell"
    assert body["round_summary"]["qty"] == 100

    assert client.get("/api/paper/replay/round", params={"symbol": "600519", "open_date": "2026-01-05", "close_date": "2026-01-02"}).status_code == 400
    assert client.get("/api/paper/replay/round", params={"symbol": "000001.SZ", "open_date": "2026-01-02", "close_date": "2026-01-05"}).status_code == 404
    assert client.get("/api/paper/replay/round", params={"symbol": "600519.SH", "open_date": "2026-01-02", "close_date": "2026-01-05", "account": "missing"}).status_code == 404
