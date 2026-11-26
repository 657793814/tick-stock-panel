import polars as pl
META = {
    "id": "legacy_alerts",
    "name": "legacy_alerts",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)

ALERTS = [{"field": "rsi_14", "op": "<", "value": 25}]
