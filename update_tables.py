"""변경된 CSV 3종(cart_items, purchase_history, search_history)을 MySQL 에 업서트.

upload_to_mysql.py 와 달리 테이블을 DROP 하지 않고, PRIMARY KEY 기준으로
INSERT ... ON DUPLICATE KEY UPDATE 를 수행한다 → 중복 없이 신규 추가 + 기존 갱신.
"""
import os
import sys

import pymysql

from upload_to_mysql import (
    DATABASE,
    HOST,
    PASSWORD,
    PORT,
    PRIMARY_KEYS,
    USER,
    build_create_sql,
    coerce_value,
    read_csv,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TARGET_TABLES = ["cart_items", "purchase_history", "search_history"]
BATCH_SIZE = 500


def upsert_table(cur, table: str, csv_path: str) -> int:
    headers, rows = read_csv(csv_path)

    # 테이블이 없으면 생성(있으면 그대로 둠 → 기존 데이터 보존)
    cur.execute(build_create_sql(table, headers))

    if not rows:
        return 0

    pk = set(PRIMARY_KEYS.get(table, []))
    col_list = ", ".join(f"`{c}`" for c in headers)
    placeholders = ", ".join(["%s"] * len(headers))
    # PK 가 아닌 컬럼만 갱신 대상으로
    update_cols = [c for c in headers if c not in pk] or headers
    update_clause = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in update_cols)
    sql = (
        f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )

    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        data = [
            tuple(coerce_value(v, table, headers[j]) for j, v in enumerate(row))
            for row in batch
        ]
        cur.executemany(sql, data)
        total += len(batch)
    return total


def main():
    print(f"Connecting to {HOST}:{PORT} / {DATABASE} …")
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=DATABASE,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=30,
    )
    cur = conn.cursor()

    for table in TARGET_TABLES:
        path = os.path.join(DATA_DIR, f"{table}.csv")
        if not os.path.exists(path):
            print(f"  [{table}] CSV 없음 — 건너뜀")
            continue
        before = _count(cur, table)
        print(f"  [{table}] ", end="", flush=True)
        try:
            n = upsert_table(cur, table, path)
            conn.commit()
            after = _count(cur, table)
            print(f"{n:,}행 처리 · 테이블 {before:,} → {after:,}행")
        except Exception as exc:
            conn.rollback()
            print(f"ERROR: {exc}")
            sys.exit(1)

    cur.close()
    conn.close()
    print("\n✅ 업서트 완료 (중복 없음 — PRIMARY KEY 기준)")


def _table_exists(cur, table: str) -> bool:
    cur.execute("SHOW TABLES LIKE %s", (table,))
    return cur.fetchone() is not None


def _count(cur, table: str) -> int:
    if not _table_exists(cur, table):
        return 0
    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
    return cur.fetchone()[0]


if __name__ == "__main__":
    main()
