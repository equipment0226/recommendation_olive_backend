"""데이터 접근 계층.

CSV(데모)와 MySQL(실서비스)을 동일한 인터페이스(`query`)로 추상화한다.
- CSV 백엔드: data/*.csv 를 인메모리 SQLite 에 적재하고 실제 SQL 을 실행한다.
  → 지금 단계에서 "CSV 를 통한 query 수행 + data 적재 검증" 목적.
- MySQL 백엔드: pymysql 로 동일한 SQL 을 실행한다.
  → CSV 의 table 과 동일 구성의 Web DB 로 이전할 때 이 클래스만 교체하면 된다.

쿼리는 두 엔진 모두에서 동작하도록 표준 SQL 로 작성한다.
플레이스홀더는 `:name` (named) 스타일로 통일하고, MySQL 용으로 변환한다.
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import threading
from typing import Any

import config

# CSV 파일명 → 테이블명. 컬럼은 CSV 헤더를 그대로 사용한다.
TABLES = [
    "users",
    "categories",
    "products",
    "purchase_history",
    "cart_items",
    "search_history",
    "search_purchase_pattern",
    "ingredient_concerns",
    "ingredient_trends",
]

# 정수/실수로 캐스팅할 컬럼 (CSV 는 전부 문자열이므로 SQL 비교·집계를 위해 형변환)
INT_COLUMNS = {
    "volume_ml", "price", "avg_lifespan_days", "is_consumable",
    "lifespan_per_100ml", "quantity", "paid_price", "days_in_cart",
    "purchase_count", "search_volume",
}
FLOAT_COLUMNS = {
    "daily_usage_ml", "conversion_rate", "trend_delta",
}


class Database:
    """SQL 실행 인터페이스. backend 에 따라 SQLite/MySQL 을 사용한다."""

    def __init__(self) -> None:
        self.backend = config.DATA_BACKEND
        self._lock = threading.Lock()
        if self.backend == "csv":
            self._conn = self._build_sqlite_from_csv()
        elif self.backend == "mysql":
            self._conn = None  # 요청마다 커넥션 생성
        else:
            raise ValueError(f"알 수 없는 DATA_BACKEND: {self.backend}")

    # ── CSV → SQLite 적재 ────────────────────────────────────────
    def _build_sqlite_from_csv(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for table in TABLES:
            path = os.path.join(config.DATA_DIR, f"{table}.csv")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader)
                rows = list(reader)
            col_defs = []
            for col in header:
                if col in INT_COLUMNS:
                    col_defs.append(f'"{col}" INTEGER')
                elif col in FLOAT_COLUMNS:
                    col_defs.append(f'"{col}" REAL')
                else:
                    col_defs.append(f'"{col}" TEXT')
            cur.execute(f'CREATE TABLE "{table}" ({", ".join(col_defs)})')
            placeholders = ", ".join(["?"] * len(header))
            typed_rows = [
                [self._cast(col, val) for col, val in zip(header, row)]
                for row in rows
            ]
            cur.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})', typed_rows
            )
        conn.commit()
        return conn

    @staticmethod
    def _cast(col: str, val: str) -> Any:
        if val == "" or val is None:
            return None
        if col in INT_COLUMNS:
            try:
                return int(val)
            except ValueError:
                return None
        if col in FLOAT_COLUMNS:
            try:
                return float(val)
            except ValueError:
                return None
        return val

    # ── 쿼리 실행 ────────────────────────────────────────────────
    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """named 플레이스홀더(:name) SQL 을 실행하고 dict 리스트를 반환한다."""
        params = params or {}
        if self.backend == "csv":
            with self._lock:
                cur = self._conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        return self._query_mysql(sql, params)

    def _query_mysql(self, sql: str, params: dict) -> list[dict]:
        import pymysql  # 지연 임포트: CSV 데모에서는 불필요

        # :name → %(name)s 로 변환 (pymysql named 스타일)
        mysql_sql = re.sub(r":(\w+)", r"%(\1)s", sql)
        conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **config.MYSQL)
        try:
            with conn.cursor() as cur:
                cur.execute(mysql_sql, params)
                return list(cur.fetchall())
        finally:
            conn.close()


# 싱글톤 인스턴스
_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
