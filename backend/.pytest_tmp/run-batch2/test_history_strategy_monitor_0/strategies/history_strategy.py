import polars as pl

META = {
    "id": "history_strategy",
    "name": "历史策略",
    "basic_filter": {"exclude_st": True},
}
LOOKBACK_DAYS = 2

def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df
