from quiv import Quiv

scheduler = Quiv(
    pool_size=4,
    history_retention_seconds=7200,
    timezone="America/New_York",
)
