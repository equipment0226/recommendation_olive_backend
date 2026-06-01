"""애플리케이션 설정.

CSV(데모) → MySQL(실서비스) 전환 시 이 파일의 값만 바꾸면 된다.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# ── 데이터 소스 선택 ────────────────────────────────────────────────
# "csv"   : data/*.csv 를 인메모리 SQLite 로 적재 후 SQL 쿼리
# "mysql" : pymysql 로 실제 MySQL Web DB 에 접속 (Railway)
DATA_BACKEND = os.environ.get("DATA_BACKEND", "mysql")

# MySQL 접속 정보 (Railway)
MYSQL = {
    "host": os.environ.get("MYSQL_HOST", "autorack.proxy.rlwy.net"),
    "port": int(os.environ.get("MYSQL_PORT", "36898")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "LxnvsIMiwXYLSSiEqtUFMbfaowfPjJTm"),
    "database": os.environ.get("MYSQL_DB", "railway"),
    "charset": "utf8mb4",
}

# ── 추천 로직 파라미터 (기획서 v0.3 / 화면설계서 v0.1) ──────────────
# 데모는 현재 계절을 'spring' 으로 고정한다(HTML 프로토타입과 동일 결과).
# 실서비스는 현재 월 → 계절 매핑으로 동적 판정.
CURRENT_SEASON = os.environ.get("CURRENT_SEASON", "spring")

# 방치 감지 기준: cart_items.days_in_cart >= STALE_DAYS  (정책서 분기1)
STALE_DAYS = 30

# 보관 버킷: 동일 상품 반복 구매 N회 이상 (정책서 4.4)
REPEAT_PURCHASE_MIN = 2

# 고민 버킷 decay: 마지막 관심 신호로부터 N일 초과 시 충동 재분류 (정책서 4.5)
INTEREST_DECAY_DAYS = 45

# STEP3 추천 결과 노출 개수
REC_LIMIT = 4
