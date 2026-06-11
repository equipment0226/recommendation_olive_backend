"""STEP 1·2 — 버킷 분류 로직 (기획서 v0.3 4장 / 화면설계서 S01·S02).

상품(cart row) 단위로 판별하며, 우선순위 결정트리를 **SQL 한 문장**으로 수행한다.
다중 조건 교차 판정(반복구매·니즈해결·시즌·관심신호)을 모두 CASE/EXISTS 로 옮겨
파이썬 분류 로직을 제거했다. 별도 정답 라벨(expected_bucket) 없이, 조회 시점에
SQL CASE 로 실시간 판별한 값 자체가 정답이다.

결정트리:
  1) 동일상품 반복구매 N회+ → '보관'
  2) 담은 후 동일 카테고리 구매 → '클렌징_니즈해결'
  3) 시즌 미스매치 → 관심신호(클릭) 있으면 '고민' else '클렌징_시즌'
  4) 관심신호(클릭 or 카테고리 검색) + 배너/SNS 유입 아님 → '고민'
  5) 그 외 → '충동'

관심신호(클릭)는 45일 Decay(마지막 검색 45일 이내)만 유효하며, 배너/SNS(event_banner/
sns_ad) 유입은 관심신호가 있어도 충동으로 본다(기획서 v0.3 충동/고민 정의).
"""
from __future__ import annotations

import config
from .db import get_db

# ── 내부 버킷 → 프론트 표시 메타 ─────────────────────────────────────
# group: cleansing(삭제 제안) / keep(유지)
# type : 프론트 배지·그룹 키 (충동/해결/시즌/고민/보관)
BUCKET_META = {
    "충동":         {"group": "cleansing", "type": "충동", "default_checked": True},
    "클렌징_니즈해결": {"group": "cleansing", "type": "해결", "default_checked": True},
    "클렌징_시즌":   {"group": "cleansing", "type": "시즌", "default_checked": True},
    "고민":         {"group": "keep",      "type": "고민", "default_checked": False},
    "보관":         {"group": "keep",      "type": "보관", "default_checked": False},
}

SEASON_KO = {"spring": "봄", "summer": "여름", "fall": "가을", "winter": "겨울"}

# 현재 계절을 **SQL 에서** 산출한다(SQL문서 2.6 season_now 동일 규칙).
# 월(MONTH(NOW())) → 계절 매핑을 SQL CASE 로 옮겨 파이썬 날짜 판정을 제거했다.
# config.CURRENT_SEASON 이 비어 있으면(기본) 이 식이 정답을 주고, 값이 있으면
# COALESCE 로 그 값(고정 계절, 데모/테스트용 override)이 우선한다.
SQL_SEASON_NOW = """CASE MONTH(NOW())
            WHEN 3 THEN 'spring' WHEN 4 THEN 'spring' WHEN 5 THEN 'spring'
            WHEN 6 THEN 'summer' WHEN 7 THEN 'summer' WHEN 8 THEN 'summer'
            WHEN 9 THEN 'fall'   WHEN 10 THEN 'fall'  WHEN 11 THEN 'fall'
            ELSE 'winter'
        END"""

# 현재 계절 단독 조회(방치 cart 가 없을 때의 표시용 폴백)
SQL_CURRENT_SEASON = f"SELECT COALESCE(:season, {SQL_SEASON_NOW}) AS season"


# ── SQL 조회 ─────────────────────────────────────────────────────────
# 방치 cart row 를 조회하면서 버킷까지 SQL 결정트리(CASE)로 한 번에 판별한다.
# [성능 최적화] CASE 안의 상관 서브쿼리(EXISTS/COUNT)를 전부 제거하고, 사용자 단위로
# 1회 집계한 파생 CTE 4개를 메인 FROM 절에 LEFT JOIN 한다. 옵티마이저가 행마다 반복
# 실행하던 서브쿼리 대신, 작은 집계 결과를 Hash/Nested-Loop Join 으로 일괄 결합한다.
#   - pc          : (상품별) 반복구매 횟수 — 행별 COUNT 서브쿼리 제거
#   - cat_purchase: (카테고리별) 최근 구매시점 MAX — EXISTS(담은 후 구매)를 비교식으로 환산
#   - clk         : 클릭한 상품 집합 — product_clicked EXISTS 제거
#   - kw_cat      : 검색어가 카테고리명을 포함하는 카테고리 집합 — LIKE 조인을
#                   행별이 아닌 "사용자 검색어 × 카테고리" 1회로 축소
# 모든 파생 CTE 는 WHERE user_id = :user_id 로 모수를 사용자 단위로 선제 축소(I/O ↓).
# 분기 우선순위·판정 의미는 기존과 100% 동일하다.
SQL_STALE_CART = f"""
WITH pc AS (
    -- 반복구매 횟수: 상품별 1회만 집계(행별 COUNT 서브쿼리 대체)
    SELECT product_id, COUNT(*) AS cnt
    FROM   purchase_history
    WHERE  user_id = :user_id
    GROUP BY product_id
),
cat_purchase AS (
    -- 담은 후 동일 카테고리 구매 EXISTS → 카테고리별 '최근 구매시점' MAX 로 환산
    SELECT pp.category_id, MAX(ph.purchased_at) AS last_purchased
    FROM   purchase_history ph
    JOIN   products pp ON pp.product_id = ph.product_id
    WHERE  ph.user_id = :user_id
    GROUP BY pp.category_id
),
clk AS (
    -- 클릭(product_clicked)한 상품 집합: EXISTS 대체용 distinct 키
    -- 45일 Decay: 마지막 관심신호로부터 45일 이내 클릭만 유효(기획서: 관심신호 45일 이내).
    -- 오래된 클릭 이력이 지금까지 '고민'으로 잡히는 것을 막는다.
    SELECT DISTINCT product_clicked AS product_id
    FROM   search_history
    WHERE  user_id = :user_id AND product_clicked IS NOT NULL
      AND  searched_at >= DATE_SUB(NOW(), INTERVAL 45 DAY)
),
kw_cat AS (
    -- 검색어가 카테고리명을 포함하는 카테고리: LIKE 조인을 사용자 검색어 집합 ×
    -- 카테고리로 1회만 수행해 행별 LIKE 상관 서브쿼리를 제거
    SELECT DISTINCT cat2.category_id
    FROM   search_history sh
    JOIN   categories cat2
        ON sh.search_keyword LIKE CONCAT('%', cat2.category_name, '%')
    WHERE  sh.user_id = :user_id
)
SELECT  c.cart_id, c.user_id, c.product_id, c.added_at, c.days_in_cart,
        c.referrer, c.quantity,
        p.product_name, p.brand, p.category_id, p.key_ingredients,
        p.suitable_season, p.texture, p.volume_ml, p.volume_unit, p.price,
        cat.category_name, cat.avg_lifespan_days,
        COALESCE(:season, {SQL_SEASON_NOW}) AS cur_season,
        CASE
            -- 1) 동일 상품 반복구매 N회+ → 보관
            WHEN COALESCE(pc.cnt, 0) >= :repeat_min
                THEN '보관'
            -- 2) 담은 후(added_at 이후) 동일 카테고리 구매 → 니즈해결
            WHEN cat_purchase.last_purchased > c.added_at
                THEN '클렌징_니즈해결'
            -- 3) 시즌 미스매치 → 클릭 신호 있으면 고민, 없으면 시즌
            WHEN p.suitable_season NOT IN ('all', COALESCE(:season, {SQL_SEASON_NOW}))
                THEN CASE
                        WHEN clk.product_id IS NOT NULL THEN '고민'
                        ELSE '클렌징_시즌'
                     END
            -- 4) 관심신호(클릭 OR 카테고리명 검색) → 고민
            --    단, 배너/SNS 유입(event_banner/sns_ad)은 관심신호가 있어도 충동으로 본다
            --    (기획서: 충동 = 이벤트/SNS 유입 + 검색 이력 없음)
            WHEN (clk.product_id IS NOT NULL OR kw_cat.category_id IS NOT NULL)
                 AND c.referrer NOT IN ('event_banner', 'sns_ad')
                THEN '고민'
            -- 5) 그 외 → 충동
            ELSE '충동'
        END AS bucket
FROM    cart_items c
JOIN    products   p   ON p.product_id  = c.product_id
JOIN    categories cat ON cat.category_id = p.category_id
LEFT JOIN pc           ON pc.product_id           = c.product_id
LEFT JOIN cat_purchase ON cat_purchase.category_id = p.category_id
LEFT JOIN clk          ON clk.product_id          = c.product_id
LEFT JOIN kw_cat       ON kw_cat.category_id      = p.category_id
WHERE   c.user_id = :user_id
  AND   c.days_in_cart >= :stale_days
ORDER BY c.days_in_cart DESC
"""


def _build_reason(bucket: str, row: dict) -> str:
    """버킷별 사용자용 사유 문구 (화면설계서 문구 방향 반영)."""
    season = SEASON_KO.get(row["suitable_season"], row["suitable_season"])
    now = SEASON_KO.get(row["cur_season"], row["cur_season"])
    days = row["days_in_cart"]
    return {
        "충동": "검색 없이 담았고 이후 관심 신호가 없어요. "
                    f"{row['referrer']} 경로로 담은 뒤 재조회가 없어요.",
        "클렌징_니즈해결": "담은 후 같은 카테고리 상품을 이미 구매하셨어요. "
                      "비슷한 니즈가 이미 해결됐어요.",
        "클렌징_시즌": f"{season} 전용 상품인데 지금은 {now}이에요. "
                    "지금 쓰기 애매한 시즌이라 다음 시즌 알림을 권해요.",
        "고민": f"담은 지 {days}일 · 같은 카테고리를 다시 검색·조회한 관심 신호가 있어요.",
        "보관": "동일 상품을 2회 이상 반복 구매하신 이력이 있어요. "
              "소진 시점에 맞춰 재구매를 권해요.",
    }.get(bucket, "")


def classify_cart_item(row) -> str:
    """SQL 이 산출한 버킷 값을 그대로 사용한다(분류 로직은 SQL_STALE_CART 의 CASE).

    파이썬 결정트리는 제거되었고, 이 함수는 호환을 위해 SQL 결과를 반환한다.
    """
    return row["bucket"]


def current_season() -> str:
    """현재 계절을 SQL(MONTH(NOW()))로 산출해 반환한다(override 우선)."""
    db = get_db()
    row = db.query(SQL_CURRENT_SEASON, {"season": config.CURRENT_SEASON or None})
    return row[0]["season"]


def get_cart_analysis(user_id: str) -> dict:
    """유저의 방치 장바구니를 분석해 STEP1·STEP2 데이터를 반환한다.

    버킷 분류·현재 계절 판정은 SQL_STALE_CART 의 CASE 가 수행하고, 여기서는 조회
    결과를 프론트 표시 형태(메타·사유 문구)로 가공만 한다.
    """
    db = get_db()
    params = {
        "user_id": user_id,
        "stale_days": config.STALE_DAYS,
        "repeat_min": config.REPEAT_PURCHASE_MIN,
        "season": config.CURRENT_SEASON or None,  # 비어있으면 SQL 이 월 기준 동적 판정
    }

    cart_rows = db.query(SQL_STALE_CART, params)

    items = []
    for row in cart_rows:
        bucket = row["bucket"]
        meta = BUCKET_META[bucket]
        items.append({
            "cart_id": row["cart_id"],
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "brand": row["brand"],
            "category_id": row["category_id"],
            "category_name": row["category_name"],
            "price": row["price"],
            "days_in_cart": row["days_in_cart"],
            "referrer": row["referrer"],
            "suitable_season": row["suitable_season"],
            "key_ingredients": (row["key_ingredients"] or "").split("|"),
            "bucket": bucket,
            "group": meta["group"],
            "type": meta["type"],
            "default_checked": meta["default_checked"],
            "reason": _build_reason(bucket, row),
        })

    cleansing = [i for i in items if i["group"] == "cleansing"]
    keep = [i for i in items if i["group"] == "keep"]
    total = len(items)
    avg_days = round(sum(i["days_in_cart"] for i in items) / total) if total else 0

    type_count: dict[str, int] = {}
    for i in cleansing:
        type_count[i["type"]] = type_count.get(i["type"], 0) + 1

    # 현재 계절: 조회된 행이 있으면 그 값(SQL 산출), 없으면 단독 조회로 폴백
    season_now = cart_rows[0]["cur_season"] if cart_rows else current_season()

    return {
        "user_id": user_id,
        "total_items": total,
        "avg_days": avg_days,
        "cleansing_count": len(cleansing),
        "keep_count": len(keep),
        "type_count": type_count,
        "cleansing_items": cleansing,
        "keep_items": keep,
        "current_season": season_now,
    }


# ── 버킷 분포: 전체 cart_items 를 SQL CASE 로 실시간 분류해 집계 ──────────
# 별도 정답 라벨 없이 CASE 판별 결과 자체가 정답이다.
SQL_ALL_STALE_USERS = """
SELECT DISTINCT user_id
FROM   cart_items
WHERE  days_in_cart >= :stale_days
ORDER BY user_id
"""


def bucket_distribution() -> dict:
    """전체 방치 장바구니를 SQL CASE 로 실시간 분류해 버킷별 분포를 집계한다."""
    db = get_db()
    user_ids = [
        r["user_id"]
        for r in db.query(SQL_ALL_STALE_USERS, {"stale_days": config.STALE_DAYS})
    ]

    total = 0
    by_bucket: dict[str, dict] = {}

    for uid in user_ids:
        analysis = get_cart_analysis(uid)
        for item in analysis["cleansing_items"] + analysis["keep_items"]:
            bucket = item["bucket"]
            total += 1
            stat = by_bucket.setdefault(
                bucket,
                {"count": 0, "group": item["group"], "type": item["type"]},
            )
            stat["count"] += 1

    cleansing_total = sum(
        s["count"] for s in by_bucket.values() if s["group"] == "cleansing"
    )
    keep_total = sum(
        s["count"] for s in by_bucket.values() if s["group"] == "keep"
    )
    for s in by_bucket.values():
        s["ratio"] = round(100 * s["count"] / total, 1) if total else 0.0

    return {
        "total": total,
        "users": len(user_ids),
        "cleansing_total": cleansing_total,
        "keep_total": keep_total,
        "by_bucket": by_bucket,
    }
