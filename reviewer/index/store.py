from __future__ import annotations
from pathlib import Path
import psycopg

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()

class ChunkStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def init_schema(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(_SCHEMA)
            conn.commit()
