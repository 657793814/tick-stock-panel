import polars as pl
META = {
    "id": "duplicate",
    "name": "duplicate",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)
