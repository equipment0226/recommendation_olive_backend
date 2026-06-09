"""
CSV → Railway MySQL 업로더
테이블명·컬럼명 변경 없이 data/ 폴더의 CSV를 그대로 저장합니다.
"""
import csv
import os
import sys
import pymysql

# ── 접속 정보 ───────────────────────────────────────────────
HOST = "autorack.proxy.rlwy.net"
PORT = 36898
USER = "root"
PASSWORD = "LxnvsIMiwXYLSSiEqtUFMbfaowfPjJTm"
DATABASE = "railway"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# ── 컬럼별 타입 오버라이드 (자동 추론이 불확실한 컬럼) ───────
TYPE_OVERRIDES: dict[str, dict[str, str]] = {
    "cart_items": {
        "cart_id": "VARCHAR(20)",
        "user_id": "VARCHAR(20)",
        "product_id": "VARCHAR(20)",
        "added_at": "DATE",
        "days_in_cart": "INT",
        "referrer": "VARCHAR(50)",
        "quantity": "INT",
        "expected_bucket": "VARCHAR(30)",
        "expected_reason": "TEXT",
    },
    "categories": {
        "category_id": "VARCHAR(20)",
        "category_name": "VARCHAR(60)",
        "avg_lifespan_days": "INT",
        "is_consumable": "TINYINT(1)",
        "daily_usage_ml": "FLOAT",
        "lifespan_per_100ml": "FLOAT",
    },
    "ingredient_concerns": {
        "ingredient": "VARCHAR(80)",
        "concern": "VARCHAR(80)",
        "efficacy_level": "VARCHAR(20)",
        "mechanism": "TEXT",
    },
    "ingredient_trends": {
        "trend_id": "VARCHAR(20)",
        "ingredient": "VARCHAR(80)",
        "month": "VARCHAR(10)",
        "search_volume": "INT",
        "trend_delta": "FLOAT",
    },
    "products": {
        "product_id": "VARCHAR(20)",
        "category_id": "VARCHAR(20)",
        "product_name": "VARCHAR(120)",
        "brand": "VARCHAR(60)",
        "key_ingredients": "TEXT",
        "concern_target": "TEXT",
        "suitable_season": "VARCHAR(20)",
        "texture": "VARCHAR(40)",
        "volume_ml": "FLOAT",
        "volume_unit": "VARCHAR(20)",
        "price": "INT",
    },
    "purchase_history": {
        "purchase_id": "VARCHAR(20)",
        "user_id": "VARCHAR(20)",
        "product_id": "VARCHAR(20)",
        "purchased_at": "DATE",
        "quantity": "INT",
        "paid_price": "INT",
    },
    "search_history": {
        "search_id": "VARCHAR(20)",
        "user_id": "VARCHAR(20)",
        "search_keyword": "VARCHAR(120)",
        "searched_at": "DATE",
        "product_clicked": "VARCHAR(20)",
    },
    "search_purchase_pattern": {
        "pattern_id": "VARCHAR(20)",
        "search_keyword": "VARCHAR(120)",
        "product_id": "VARCHAR(20)",
        "purchase_count": "INT",
        "conversion_rate": "FLOAT",
    },
    "users": {
        "user_id": "VARCHAR(20)",
        "skin_type": "VARCHAR(40)",
        "skin_concerns": "TEXT",
        "age_group": "VARCHAR(20)",
    },
}

# 테이블별 PK 지정
PRIMARY_KEYS: dict[str, list[str]] = {
    "cart_items": ["cart_id"],
    "categories": ["category_id"],
    "ingredient_concerns": ["ingredient", "concern"],
    "ingredient_trends": ["trend_id"],
    "products": ["product_id"],
    "purchase_history": ["purchase_id"],
    "search_history": ["search_id"],
    "search_purchase_pattern": ["pattern_id"],
    "users": ["user_id"],
}

BATCH_SIZE = 500  # INSERT 배치 크기

# ── 인덱스 설계 (SQL문서 6장) ─────────────────────────────────
# 버킷 판별·추천 쿼리가 자주 쓰는 조회 패턴을 가속한다.
#   - cart_items   : 유저별 방치 상품 조회, 유저+상품 반복구매 조인
#   - purchase_history: 유저+상품+날짜(반복구매·니즈해결), 유저+날짜
#   - search_history  : 유저+날짜, 키워드 LIKE 매칭
#   - search_purchase_pattern: 키워드별 전환율 정렬, 상품 조인
#   - products / ingredient_trends: 카테고리·성분·월 조회
INDEXES: dict[str, list[tuple[str, str]]] = {
    "cart_items": [
        # ORDER BY days_in_cart DESC 를 그대로 타도록 내림차순 인덱스(MySQL 8.0+) 적용
        ("idx_cart_user", "(user_id, days_in_cart DESC)"),
        ("idx_cart_user_prod", "(user_id, product_id)"),
    ],
    "purchase_history": [
        ("idx_pu_user_prod_date", "(user_id, product_id, purchased_at)"),
        ("idx_pu_user_date", "(user_id, purchased_at)"),
    ],
    "search_history": [
        ("idx_sh_user_date", "(user_id, searched_at)"),
        ("idx_sh_user_clicked", "(user_id, product_clicked)"),
        ("idx_sh_keyword", "(search_keyword)"),
    ],
    "search_purchase_pattern": [
        ("idx_spp_kw_conv", "(search_keyword, conversion_rate)"),
        ("idx_spp_product", "(product_id)"),
    ],
    "products": [
        ("idx_prod_category", "(category_id)"),
    ],
    "ingredient_trends": [
        ("idx_trend_month", "(month, trend_delta)"),
        ("idx_trend_ing_month", "(ingredient, month)"),
    ],
}


def create_indexes(cur) -> int:
    """INDEXES 정의대로 인덱스를 생성한다(이미 있으면 건너뀜).

    MySQL 은 'CREATE INDEX IF NOT EXISTS' 를 지원하지 않으므로
    information_schema 로 존재 여부를 확인한 뒤 생성한다.
    """
    made = 0
    for table, idxs in INDEXES.items():
        for name, cols in idxs:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = %s AND table_name = %s AND index_name = %s",
                (DATABASE, table, name),
            )
            if cur.fetchone()[0]:
                continue
            cur.execute(f"CREATE INDEX `{name}` ON `{table}` {cols}")
            made += 1
    return made


def read_csv(path: str) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)
    return headers, rows


def build_create_sql(table: str, columns: list[str]) -> str:
    col_defs = []
    overrides = TYPE_OVERRIDES.get(table, {})
    for col in columns:
        dtype = overrides.get(col, "TEXT")
        col_defs.append(f"  `{col}` {dtype}")
    pk = PRIMARY_KEYS.get(table, [])
    if pk:
        pk_str = ", ".join(f"`{k}`" for k in pk)
        col_defs.append(f"  PRIMARY KEY ({pk_str})")
    cols_sql = ",\n".join(col_defs)
    return (
        f"CREATE TABLE IF NOT EXISTS `{table}` (\n{cols_sql}\n) "
        f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
    )


def coerce_value(val: str, table: str, col: str):
    """빈 문자열을 None 으로 변환. 숫자 타입도 Python 네이티브로."""
    if val == "" or val is None:
        return None
    dtype = TYPE_OVERRIDES.get(table, {}).get(col, "TEXT").upper()
    if dtype in ("INT", "TINYINT(1)"):
        try:
            return int(val)
        except ValueError:
            return None
    if dtype in ("FLOAT", "DOUBLE"):
        try:
            return float(val)
        except ValueError:
            return None
    return val


def upload_table(cur, table: str, csv_path: str) -> int:
    headers, rows = read_csv(csv_path)

    # CREATE TABLE
    create_sql = build_create_sql(table, headers)
    cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    cur.execute(create_sql)

    if not rows:
        return 0

    placeholders = ", ".join(["%s"] * len(headers))
    col_list = ", ".join(f"`{c}`" for c in headers)
    insert_sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"

    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        data = [
            tuple(coerce_value(v, table, headers[j]) for j, v in enumerate(row))
            for row in batch
        ]
        cur.executemany(insert_sql, data)
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

    csv_files = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".csv"))
    for fname in csv_files:
        table = fname[:-4]  # 확장자 제거
        path = os.path.join(DATA_DIR, fname)
        print(f"  [{table}] ", end="", flush=True)
        try:
            n = upload_table(cur, table, path)
            conn.commit()
            print(f"{n:,}행 완료")
        except Exception as exc:
            conn.rollback()
            print(f"ERROR: {exc}")
            sys.exit(1)

    # 테이블 적재 후 인덱스 생성(조회 속도 최적화)
    print("  [indexes] ", end="", flush=True)
    try:
        made = create_indexes(cur)
        conn.commit()
        print(f"{made}개 생성(이미 있는 건 건너뜀)")
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: {exc}")
        sys.exit(1)

    cur.close()
    conn.close()
    print("\n✅ 전체 업로드 완료")


if __name__ == "__main__":
    main()
