"""腾讯公开行情插件定向测试: 字段口径、代码映射、fail-closed、门禁。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.api import kline as kline_api
from app.data_providers.base import ProviderRequestError
from app.plugins.tencent import client as tclient
from app.plugins.tencent.provider import TencentProvider, _minute_rows, _real_row


def test_code_mapping_for_all_markets():
    assert tclient.to_code("600519.SH") == "sh600519"
    assert tclient.to_code("000001.SZ") == "sz000001"
    assert tclient.to_code("300750.SZ") == "sz300750"
    assert tclient.to_code("920002.BJ") == "bj920002"
    assert tclient.to_code("688981.SH") == "sh688981"
    assert tclient.to_code("600519") == "sh600519"
    assert tclient.to_code("00700.HK") == "hk00700"
    assert tclient.from_code("sh000001") == ("000001.SH", "index")
    assert tclient.from_code("sz399001") == ("399001.SZ", "index")
    assert tclient.from_code("hk00700") == ("00700.HK", "stock")
    assert tclient.to_code("invalid") is None


def test_minute_datetime_parse():
    assert tclient.minute_datetime("202611271500") == datetime(2026, 11, 27, 15, 0)
    assert tclient.minute_datetime("bad") is None


def test_minute_rows_field_order_and_units():
    raw = [[
        "202611271500", "1475.60", "1472.36", "1475.60", "1472.36", "446.00", {}, "0.36",
    ]]
    rows = _minute_rows("600519.SH", raw, None, None)
    assert len(rows) == 1
    row = rows[0]
    # 上游行序: 时间, 开, 收, 高, 低, 量(手); 收在第三位
    assert row["close"] == 1472.36
    assert row["open"] == 1475.60
    assert row["high"] == 1475.60
    assert row["low"] == 1472.36
    assert row["volume"] == 446.0
    assert row["amount"] == pytest.approx(446.0 * 100 * 1472.36)
    assert row["datetime"] == datetime(2026, 11, 27, 15, 0)


def test_minute_rows_window_filter_and_malformed():
    raw = [
        ["202611271500", "10", "11", "12", "9", "100", {}, "1"],
        ["202611261500", "10", "11", "12", "9", "100", {}, "1"],
        ["bad", "10", "11", "12", "9", "100", {}, "1"],
        ["202611271501"],
    ]
    start = datetime(2026, 11, 27)
    rows = _minute_rows("600519.SH", raw, start, None)
    assert len(rows) == 1
    assert rows[0]["datetime"] == datetime(2026, 11, 27, 15, 0)


def _quote_fields() -> list[str]:
    """构造 qt.gtimg.cn 字段向量, 索引与真实接口一致 (共 40 位)。"""
    fields = ["0"] * 40
    fields[0:7] = ["1", "贵州茅台", "600519", "1472.36", "1469.00", "1469.99", "39808"]
    fields[30] = "20261127161454"
    fields[31] = "3.36"
    fields[32] = "0.23"
    fields[33] = "1481.41"
    fields[34] = "1452.99"
    fields[37] = "584911"
    fields[38] = "0.32"
    return fields


def test_quote_fields_gbk_shaped():
    row = _real_row("sh600519", _quote_fields(), 1700000000000)
    assert row is not None
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 1472.36
    assert row["prev_close"] == 1469.0
    assert row["volume"] == 39808.0
    assert row["amount"] == pytest.approx(584911 * 10000)
    assert row["change_pct"] == pytest.approx(0.0023)
    assert row["turnover_rate"] == pytest.approx(0.0032)


def test_quote_rejects_short_fields():
    assert _real_row("sh600519", ["1", "name"], 0) is None
    assert _real_row("unknown", ["1"] * 40, 0) is None


def test_client_quote_decodes_gbk(monkeypatch):
    payload = ('v_sh600519="' + "~".join(_quote_fields()) + '";\n').encode("gbk")

    class Resp:
        content = payload
        status_code = 200

    monkeypatch.setattr(tclient.TencentClient, "_get", lambda self, url, params=None: Resp())
    rows = tclient.TencentClient().quote(["600519.SH"])
    assert len(rows) == 1
    assert rows[0]["code"] == "sh600519"
    assert rows[0]["fields"][1] == "贵州茅台"


def test_provider_raw_unknown_kind_fails_closed():
    with pytest.raises(tclient.TencentError):
        TencentProvider().raw("unknown", {})


def test_provider_minute_request_error_becomes_provider_error(monkeypatch):
    provider = TencentProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: SimpleNamespace(
        minute_kline=lambda *a, **k: (_ for _ in ()).throw(tclient.TencentError("boom"))
    ))
    with pytest.raises(ProviderRequestError):
        list(provider.iter_minute(["600519.SH"], None, None))


def test_provider_minute_empty_returns_empty_frame(monkeypatch):
    provider = TencentProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: SimpleNamespace(minute_kline=lambda *a, **k: []))
    frame = provider.get_minute(["600519.SH"], None, None)
    assert frame.is_empty()


def test_minute_gate_allows_tencent_plugin(monkeypatch):
    from app.services import preferences

    class NoCap:
        def has(self, _cap):
            return False

    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tencent")
    assert kline_api._minute_allowed(NoCap()) is True
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "fuyao")
    assert kline_api._minute_allowed(NoCap()) is False


# ---------------------------------------------------------------------------
# realtime / depth5 / daily 路由数据集 (字段索引对照 2026-11-27 实测报文)
# ---------------------------------------------------------------------------

def _depth_quote_fields() -> list[str]:
    """带五档的字段向量: 买一~买五 = 9-18, 卖一~卖五 = 19-28 (价,量交替)。"""
    fields = _quote_fields()
    fields[9:29] = [
        "1472.32", "1", "1472.31", "2", "1472.30", "2", "1472.26", "19", "1472.25", "7",
        "1472.36", "2", "1473.00", "6", "1473.03", "2", "1473.60", "3", "1473.68", "2",
    ]
    return fields


def _quote_provider(monkeypatch, fields: list[str]) -> TencentProvider:
    """client.quote 接收项目 symbol (内部转腾讯代码), fake 按同样约定回包。"""
    provider = TencentProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: SimpleNamespace(
        quote=lambda symbols: [
            {"code": (tclient.to_code(s) or ""), "fields": fields} for s in symbols
        ],
        daily_kline=lambda code, count=120, fq="qfq": _daily_raw(),
    ))
    return provider


def _daily_raw() -> list[list]:
    """fqkline 不复权行: [日期, 开, 收, 高, 低, 量(手)] — 收在第三位。"""
    return [
        ["2026-11-26", "1470.00", "1469.00", "1473.00", "1456.00", "32408.00"],
        ["2026-11-27", "1469.99", "1472.36", "1481.41", "1452.99", "39808.00"],
        ["2026-11-28", "1472.00", "1475.00", "1478.00", "1470.00", "31000.00"],
    ]


def test_get_realtime_requires_symbol_list():
    """腾讯无法枚举全市场: 不带标的调用必须明确抛错 (fail-closed), 不静默返回空。"""
    provider = TencentProvider()
    with pytest.raises(ProviderRequestError):
        provider.get_realtime()


def test_get_realtime_batch_parses_fields(monkeypatch):
    provider = _quote_provider(monkeypatch, _quote_fields())
    rows = provider.get_realtime(["600519.SH"])
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 1472.36
    assert row["volume"] == 39808.0          # 手
    assert row["amount"] == pytest.approx(584911 * 10_000.0)  # 万元 → 元
    assert row["change_pct"] == pytest.approx(0.0023)          # 百分数 → 小数制
    assert row["turnover_rate"] == pytest.approx(0.0032)


def test_get_realtime_symbols_and_indices_share_quote_path(monkeypatch):
    provider = _quote_provider(monkeypatch, _quote_fields())
    assert provider.get_realtime_symbols(["600519.SH"])[0]["symbol"] == "600519.SH"
    indices = provider.get_realtime_indices(["000001.SH"])
    assert indices is not None and indices[0]["symbol"] == "000001.SH"


def test_depth_batch_five_levels(monkeypatch):
    provider = _quote_provider(monkeypatch, _depth_quote_fields())
    books = provider.get_depth_batch(["600519.SH"])
    book = books["600519.SH"]
    assert book["bid_prices"][0] == 1472.32
    assert book["bid_volumes"][0] == 1.0
    assert book["ask_prices"][0] == 1472.36
    assert book["ask_volumes"][4] == 2.0
    assert len(book["bid_prices"]) == 5 and len(book["ask_prices"]) == 5
    assert book["timestamp"] is not None


def test_depth_batch_skips_rows_without_levels(monkeypatch):
    provider = _quote_provider(monkeypatch, _quote_fields())  # 五档字段全 0 → 视为无盘口
    assert provider.get_depth_batch(["600519.SH"]) == {}


def test_daily_rows_field_order_units_and_window():
    from app.plugins.tencent.provider import _daily_rows

    start, end = datetime(2026, 11, 26).date(), datetime(2026, 11, 27).date()
    rows = _daily_rows("600519.SH", _daily_raw(), start, end)
    assert [r["date"].isoformat() for r in rows] == ["2026-11-26", "2026-11-27"]
    row = rows[-1]
    # 行序 [日期, 开, 收, 高, 低, 量]: 收在第三位
    assert row["close"] == 1472.36
    assert row["open"] == 1469.99
    assert row["high"] == 1481.41
    assert row["low"] == 1452.99
    assert row["volume"] == 39808.0
    assert row["amount"] == pytest.approx(39808.0 * 100 * 1472.36)  # 量×价推算


def test_iter_daily_slices_long_windows(monkeypatch):
    """长窗口按日历日切片, 单请求 count 不超过上游 700 根上限; fq 恒为不复权。"""
    from datetime import timedelta

    provider = _quote_provider(monkeypatch, _quote_fields())
    calls: list[tuple[int, str]] = []

    def fake_daily_kline(code, count=120, fq="qfq"):
        calls.append((count, fq))
        return _daily_raw()

    monkeypatch.setattr(provider, "_get_client", lambda: SimpleNamespace(daily_kline=fake_daily_kline))
    end = datetime(2026, 11, 27)
    start = end - timedelta(days=1000)
    frame = provider.get_daily(["600519.SH"], start, end)
    assert not frame.is_empty()
    assert len(calls) == 3  # 1000 天 → 450/450/100 三片
    assert all(count <= 700 for count, _ in calls)
    assert all(fq == "" for _, fq in calls)  # 路由口径: 不复权
    assert set(frame["symbol"].unique().to_list()) == {"600519.SH"}
