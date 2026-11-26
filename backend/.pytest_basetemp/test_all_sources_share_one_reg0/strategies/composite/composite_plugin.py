import polars as pl
META = {
    "id": "composite_plugin",
    "name": "composite_plugin",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)
