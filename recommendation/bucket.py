"""STEP 1·2 — 버킷 분류 로직 (기획서 v0.3 4장 / 화면설계서 S01·S02).

상품(cart row) 단위로 판별하며, 우선순위 결정트리를 적용한다.
모든 원천 데이터는 SQL 로 조회하고(= CSV/MySQL 공통), 다중 조건 교차 판정만
파이썬에서 수행한다. expected_bucket 컬럼과 대조해 검증할 수 있다.

결정트리(검증 일치율 96%):
  1) 동일상품 반복구매 N회+ → '보관'
  2) 담은 후 동일 카테고리 구매 → '클렌징_니즈해결'
  3) 시즌 미스매치 → 관심신호(클릭) 있으면 '고민' else '클렌징_시즌'
  4) 관심신호(클릭 or 카테고리 검색) → '고민'
  5) 그 외 → '충동'
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


# ── SQL 조회 ─────────────────────────────────────────────────────────
SQL_STALE_CART = """
SELECT  c.cart_id, c.user_id, c.product_id, c.added_at, c.days_in_cart,
        c.referrer, c.quantity, c.expected_bucket,
        p.product_name, p.brand, p.category_id, p.key_ingredients,
        p.suitable_season, p.texture, p.volume_ml, p.volume_unit, p.price,
        cat.category_name, cat.avg_lifespan_days
FROM    cart_items c
JOIN    products   p   ON p.product_id  = c.product_id
JOIN    categories cat ON cat.category_id = p.category_id
WHERE   c.user_id = :user_id
  AND   c.days_in_cart >= :stale_days
ORDER BY c.days_in_cart DESC
"""

# 동일 상품 반복 구매 횟수 (정책서 4.4 보관 기준)
SQL_REPEAT_COUNT = """
SELECT  product_id, COUNT(*) AS cnt
FROM    purchase_history
WHERE   user_id = :user_id
GROUP BY product_id
"""

# 유저의 구매 이력 (카테고리·구매일 포함) — 니즈해결 판정용
SQL_USER_PURCHASES = """
SELECT  ph.product_id, ph.purchased_at, p.category_id
FROM    purchase_history ph
JOIN    products p ON p.product_id = ph.product_id
WHERE   ph.user_id = :user_id
"""

# 유저의 검색 이력 — catmatch / clicked_this 판정용
SQL_USER_SEARCHES = """
SELECT  search_keyword, product_clicked, searched_at
FROM    search_history
WHERE   user_id = :user_id
"""


def _build_reason(bucket: str, row: dict) -> str:
    """버킷별 사용자용 사유 문구 (화면설계서 문구 방향 반영)."""
    season = SEASON_KO.get(row["suitable_season"], row["suitable_season"])
    now = SEASON_KO.get(config.CURRENT_SEASON, config.CURRENT_SEASON)
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


def classify_cart_item(row, repeat_counts, purchases, searches) -> str:
    """단일 cart row 의 버킷을 결정트리로 판별한다."""
    pid = row["product_id"]
    cat = row["category_id"]
    added = row["added_at"]

    repeat2 = repeat_counts.get(pid, 0) >= config.REPEAT_PURCHASE_MIN
    samecat_after = any(
        pr["category_id"] == cat and (pr["purchased_at"] or "") > (added or "")
        for pr in purchases
    )
    season_mis = row["suitable_season"] not in ("all", config.CURRENT_SEASON)
    cat_name = row["category_name"]
    catmatch = any(cat_name in (s["search_keyword"] or "") for s in searches)
    clicked_this = any(s["product_clicked"] == pid for s in searches)

    # 우선순위 결정트리
    if repeat2:
        return "보관"
    if samecat_after:
        return "클렌징_니즈해결"
    if season_mis:
        return "고민" if clicked_this else "클렌징_시즌"
    if clicked_this or catmatch:
        return "고민"
    return "충동"


def get_cart_analysis(user_id: str) -> dict:
    """유저의 방치 장바구니를 분석해 STEP1·STEP2 데이터를 반환한다."""
    db = get_db()
    params = {"user_id": user_id, "stale_days": config.STALE_DAYS}

    cart_rows = db.query(SQL_STALE_CART, params)
    repeat_counts = {
        r["product_id"]: r["cnt"]
        for r in db.query(SQL_REPEAT_COUNT, {"user_id": user_id})
    }
    purchases = db.query(SQL_USER_PURCHASES, {"user_id": user_id})
    searches = db.query(SQL_USER_SEARCHES, {"user_id": user_id})

    items = []
    for row in cart_rows:
        bucket = classify_cart_item(row, repeat_counts, purchases, searches)
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
            # 검증용: 정답값
            "expected_bucket": row["expected_bucket"],
            "match": bucket == row["expected_bucket"],
        })

    cleansing = [i for i in items if i["group"] == "cleansing"]
    keep = [i for i in items if i["group"] == "keep"]
    total = len(items)
    avg_days = round(sum(i["days_in_cart"] for i in items) / total) if total else 0

    type_count: dict[str, int] = {}
    for i in cleansing:
        type_count[i["type"]] = type_count.get(i["type"], 0) + 1

    return {
        "user_id": user_id,
        "total_items": total,
        "avg_days": avg_days,
        "cleansing_count": len(cleansing),
        "keep_count": len(keep),
        "type_count": type_count,
        "cleansing_items": cleansing,
        "keep_items": keep,
        "current_season": config.CURRENT_SEASON,
    }


# ── 검증: 전체 cart_items 의 분류 결과 vs expected_bucket ──────────────
SQL_ALL_STALE_USERS = """
SELECT DISTINCT user_id
FROM   cart_items
WHERE  days_in_cart >= :stale_days
ORDER BY user_id
"""


def validate_all() -> dict:
    """전체 장바구니를 분류해 expected_bucket 과의 일치율을 계산한다."""
    db = get_db()
    user_ids = [
        r["user_id"]
        for r in db.query(SQL_ALL_STALE_USERS, {"stale_days": config.STALE_DAYS})
    ]

    total = 0
    correct = 0
    by_bucket: dict[str, dict] = {}
    confusion: dict[str, dict[str, int]] = {}
    mismatches = []

    for uid in user_ids:
        analysis = get_cart_analysis(uid)
        for item in analysis["cleansing_items"] + analysis["keep_items"]:
            exp = item["expected_bucket"]
            got = item["bucket"]
            total += 1
            stat = by_bucket.setdefault(exp, {"total": 0, "correct": 0})
            stat["total"] += 1
            confusion.setdefault(exp, {})
            confusion[exp][got] = confusion[exp].get(got, 0) + 1
            if item["match"]:
                correct += 1
                stat["correct"] += 1
            else:
                mismatches.append({
                    "user_id": uid,
                    "product_id": item["product_id"],
                    "product_name": item["product_name"],
                    "expected": exp,
                    "predicted": got,
                })

    rate = round(100 * correct / total, 1) if total else 0.0
    for b in by_bucket.values():
        b["rate"] = round(100 * b["correct"] / b["total"], 1) if b["total"] else 0.0

    return {
        "total": total,
        "correct": correct,
        "match_rate": rate,
        "by_bucket": by_bucket,
        "confusion": confusion,
        "mismatches": mismatches,
    }
