"""애플리케이션 설정.

CSV(데모) → MySQL(실서비스) 전환 시 이 파일의 값만 바꾸면 된다.
"""
import os
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# ── 데이터 소스 선택 ────────────────────────────────────────────────
# "csv"   : data/*.csv 를 인메모리 SQLite 로 적재 후 SQL 쿼리
# "mysql" : pymysql 로 실제 MySQL Web DB 에 접속 (Railway)
DATA_BACKEND = os.environ.get("DATA_BACKEND", "mysql")


def _parse_mysql_url(url: str) -> dict | None:
    """mysql://user:pass@host:port/db 형태의 URL 을 파싱한다.

    Railway 가 주입하는 MYSQL_URL / DATABASE_URL 을 그대로 사용하기 위함.
    내부 네트워크(mysql.railway.internal)를 쓰면 가장 안정적이다.
    """
    if not url:
        return None
    p = urlparse(url)
    if p.scheme not in ("mysql", "mysql+pymysql"):
        return None
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 3306,
        "user": p.username or "root",
        "password": p.password or "",
        "database": (p.path or "/").lstrip("/") or "railway",
        "charset": "utf8mb4",
    }


# 우선순위: MYSQL_URL > DATABASE_URL > 개별 환경변수 > 기본값(공개 프록시)
_url_cfg = _parse_mysql_url(
    os.environ.get("MYSQL_URL") or os.environ.get("DATABASE_URL") or ""
)

MYSQL = _url_cfg or {
    "host": os.environ.get("MYSQL_HOST", "autorack.proxy.rlwy.net"),
    "port": int(os.environ.get("MYSQL_PORT", "36898")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "LxnvsIMiwXYLSSiEqtUFMbfaowfPjJTm"),
    "database": os.environ.get("MYSQL_DB", "railway"),
    "charset": "utf8mb4",
}

# ── Pexels 샘플 이미지 API ──────────────────────────────────────────
PEXELS_API_KEY = os.environ.get(
    "PEXELS_API_KEY",
    "kir2AxsM9J1o8zz9wXh73W1fFE9YdG8dp829aO4r0mIu7zDw7rC3gHj4",
)

# category_id → Pexels 검색 영어 쿼리(상품 키워드). category_name + product 형태.
CATEGORY_IMAGE_QUERY = {
    "C001": "skin toner",
    "C002": "essence serum",
    "C003": "skincare cream",
    "C004": "cleansing foam",
    "C005": "sunscreen",
    "C006": "face mask sheet",
    "C007": "shampoo bottle",
    "C008": "hair treatment",
    "C009": "body lotion",
    "C010": "lip balm",
    "C011": "cushion foundation",
    "C012": "eyeshadow palette",
    "C013": "ampoule serum",
    "C014": "eye cream",
    "C015": "toner pad",
    "C016": "facial mist",
    "C017": "cleansing oil",
    "C018": "lipstick",
    "C019": "body wash",
    "C020": "hair essence",
}

# ── 추천 로직 파라미터 (기획서 v0.3 / 화면설계서 v0.1) ──────────────
# 현재 계절은 기본적으로 SQL 에서 MONTH(NOW()) 로 동적 판정한다(SQL문서 2.6 season_now).
# 빈 값("")이면 동적 판정, 특정 계절(spring/summer/fall/winter)을 넣으면 그 값으로 고정
# (데모·테스트에서 특정 시즌 결과를 재현할 때만 override 로 사용).
# ※ data/cart_items.csv 의 expected_bucket 정답 라벨은 **현재 시즌(summer) 기준**으로
#   재라벨되어 있어, 동적 판정(6월=summer)과 시즌 버킷(충동↔클렌징_시즌)이 일치한다.
CURRENT_SEASON = os.environ.get("CURRENT_SEASON", "")

# 방치 감지 기준: cart_items.days_in_cart >= STALE_DAYS  (정책서 분기1)
STALE_DAYS = 30

# 보관 버킷: 동일 상품 반복 구매 N회 이상 (정책서 4.4)
REPEAT_PURCHASE_MIN = 2

# 고민 버킷 decay: 마지막 관심 신호로부터 N일 초과 시 충동 재분류 (정책서 4.5)
INTEREST_DECAY_DAYS = 45

# STEP3 추천 결과 노출 개수
REC_LIMIT = 4
