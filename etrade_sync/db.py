import os
from pathlib import Path

import psycopg2
from etrade_sync.config import DATABASE_URL

DATA_MODEL_DIR = Path(__file__).parent.parent / "data_model"


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def create_tables():
    """Execute all data_model/*.sql files in numbered order."""
    sql_files = sorted(DATA_MODEL_DIR.glob("*.sql"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            for path in sql_files:
                cur.execute(path.read_text())
