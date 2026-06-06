"""STEP 3 — 고객 선택형 신규 추천 (기획서 v0.3 5.2 / 화면설계서 S03·S04).

4가지 추천 알고리즘을 모두 SQL 기반으로 구현한다.
  - CF (유사 고객 구매 기반): 피부타입·피부고민 유사 고객의 구매 상품
  - 트렌드: ingredient_trends 검색량 상승 성분 + search_purchase_pattern 전환율
  - 검색 의도: 유저 최근 검색 키워드 + search_purchase_pattern 전환율
  - 소진/재구매: 보관 버킷(동일 상품 반복 구매) + categories 소진 주기
추천 결과는 사용자가 카드를 선택해야 노출된다(자동 노출 X).
"""
from __future__ import annotations

import config
from .db import get_db


def _won(price) -> str:
    return f"₩{int(price):,}" if price is not None else ""


# ════════════════════════════════════════════════════════════════════
# 1. CF — 유사 고객 구매 기반 추천 (Gower 유사성 SQL)
# ════════════════════════════════════════════════════════════════════
# 유사 고객 선정은 Gower 유사도를 SQL 로 계산한다(파이썬 교집합 루프 제거).
#   - skin_type   : 대칭형 범주 피처 → 같은 그룹만 후보(하드 필터, 그룹 내 유사도 1)
#   - skin_concerns: 비대칭형 다중값 범주 피처 → 공유 고민 수(shared)로 부분 유사도
# 현재 정책(동작 보존): "같은 피부타입 AND 공유 고민 1개 이상" 인 고객을 유사로 보고,
# 그런 고객이 한 명도 없으면 같은 피부타입 전체로 폴백한다. 두 분기 모두 SQL 에서 처리.
SQL_USER = "SELECT skin_type, skin_concerns, age_group FROM users WHERE user_id = :user_id"


def _build_similar_users_sql(concerns: list[str]) -> tuple[str, dict]:
    """현재 유저의 피부 고민 목록으로 Gower 유사 고객 선정 SQL 과 파라미터를 만든다.

    각 고민을 구분자(`|`) 경계까지 정확히 매칭하는 비대칭 이진 지표로 환산해
    공유 고민 수(shared)를 SQL 에서 합산한다. 문자열 연결(||/CONCAT)을 쓰지 않아
    SQLite·MySQL 양쪽에서 동일하게 동작한다.
    """
    params: dict = {}
    indicators: list[str] = []
    for i, c in enumerate(concerns):
        if not c:
            continue
        params[f"c{i}"] = c               # 단일 고민(구분자 없음)
        params[f"c{i}_s"] = f"{c}|%"      # 맨 앞
        params[f"c{i}_e"] = f"%|{c}"      # 맨 뒤
        params[f"c{i}_m"] = f"%|{c}|%"    # 가운데
        indicators.append(
            f"CASE WHEN (u.skin_concerns = :c{i} "
            f"OR u.skin_concerns LIKE :c{i}_s "
            f"OR u.skin_concerns LIKE :c{i}_e "
            f"OR u.skin_concerns LIKE :c{i}_m) THEN 1 ELSE 0 END"
        )
    shared_expr = " + ".join(indicators) if indicators else "0"
    sql = f"""
    WITH peer_sim AS (
        SELECT  u.user_id,
                ({shared_expr}) AS shared
        FROM    users u
        WHERE   u.skin_type = :skin_type AND u.user_id <> :user_id
    )
    SELECT user_id
    FROM   peer_sim
    WHERE  CASE WHEN (SELECT MAX(shared) FROM peer_sim) > 0
                THEN shared > 0 ELSE 1 END
    """
    return sql, params


# 유사 고객들이 구매한 상품 랭킹 (이미 산 상품 제외)
SQL_CF_PRODUCTS = """
SELECT  p.product_id, p.product_name, p.brand, p.price, p.category_id, cat.category_name,
        COUNT(*) AS buyers
FROM    purchase_history ph
JOIN    products   p   ON p.product_id   = ph.product_id
JOIN    categories cat ON cat.category_id = p.category_id
WHERE   ph.user_id IN ({user_list})
  AND   ph.product_id NOT IN (
            SELECT product_id FROM purchase_history WHERE user_id = :user_id
        )
GROUP BY p.product_id, p.product_name, p.brand, p.price, p.category_id, cat.category_name
ORDER BY buyers DESC, p.price DESC
LIMIT :limit
"""


def recommend_cf(user_id: str) -> dict:
    db = get_db()
    urows = db.query(SQL_USER, {"user_id": user_id})
    if not urows:
        return _empty("유사 고객 구매 기반")
    me = urows[0]
    my_concerns = set((me["skin_concerns"] or "").split("|"))

    # Gower 유사 고객 선정을 SQL 로 수행 (피부타입 동일 + 공유 고민 ≥1, 없으면 전체 폴백)
    sim_sql, sim_params = _build_similar_users_sql(sorted(my_concerns))
    sim_params.update({"skin_type": me["skin_type"], "user_id": user_id})
    similar = [r["user_id"] for r in db.query(sim_sql, sim_params)]

    if not similar:
        return _empty("유사 고객 구매 기반")

    user_list = ", ".join(f"'{u}'" for u in similar)
    sql = SQL_CF_PRODUCTS.format(user_list=user_list)
    rows = db.query(sql, {"user_id": user_id, "limit": config.REC_LIMIT})

    peer_n = len(similar)
    items = []
    for r in rows:
        rate = round(100 * r["buyers"] / peer_n)
        items.append({
            "product_id": r["product_id"], "name": r["product_name"],
            "brand": r["brand"], "price": _won(r["price"]),
            "category_id": r["category_id"],
            "tag": f"구매율 {rate}%",
        })
    return {
        "id": "cf", "algo": "Collaborative Filtering",
        "title": "나와 비슷한 고객이 결국 뭘 샀는지 알려줄까요?",
        "desc": f"같은 피부타입·고민 고객 {peer_n}명의 선택이에요",
        "result_title": "비슷한 피부 고민 고객의 선택",
        "result_sub": f"{me['skin_type']} · {', '.join(my_concerns)} {peer_n}명 기준",
        "items": items,
    }


# ════════════════════════════════════════════════════════════════════
# 2. 트렌드 기반 추천
# ════════════════════════════════════════════════════════════════════
SQL_LATEST_MONTH = "SELECT MAX(month) AS m FROM ingredient_trends"

# 최근 달 검색량 상승 성분 (trend_delta 상위)
SQL_TREND_INGREDIENTS = """
SELECT  ingredient, search_volume, trend_delta
FROM    ingredient_trends
WHERE   month = :month AND trend_delta > 0
ORDER BY trend_delta DESC
LIMIT   8
"""


def recommend_trend(user_id: str) -> dict:
    db = get_db()
    month = db.query(SQL_LATEST_MONTH)[0]["m"]
    rising = db.query(SQL_TREND_INGREDIENTS, {"month": month})

    items = []
    seen = set()
    for ing in rising:
        # 해당 성분을 포함한 상품을 전환율 높은 순으로 매칭
        rows = db.query(
            """
            SELECT  p.product_id, p.product_name, p.brand, p.price, p.category_id,
                    MAX(spp.conversion_rate) AS conv
            FROM    products p
            LEFT JOIN search_purchase_pattern spp ON spp.product_id = p.product_id
            WHERE   p.key_ingredients LIKE :pat
            GROUP BY p.product_id, p.product_name, p.brand, p.price, p.category_id
            ORDER BY conv DESC
            LIMIT 2
            """,
            {"pat": f"%{ing['ingredient']}%"},
        )
        delta = round(ing["trend_delta"] * 100)
        for r in rows:
            if r["product_id"] in seen:
                continue
            seen.add(r["product_id"])
            items.append({
                "product_id": r["product_id"], "name": r["product_name"],
                "brand": r["brand"], "price": _won(r["price"]),
                "category_id": r["category_id"],
                "tag": f"{ing['ingredient']} ↑{delta}%",
            })
        if len(items) >= config.REC_LIMIT:
            break

    return {
        "id": "trend", "algo": "트렌드 기반 추천",
        "title": "요즘 뜨는 성분 트렌드 알려줄까요?",
        "desc": f"{month} 검색량이 급상승한 성분 기반이에요",
        "result_title": "검색량 상승 성분 트렌드 상품",
        "result_sub": f"{month} 기준 trend_delta 상위 성분 포함",
        "items": items[: config.REC_LIMIT],
    }


# ════════════════════════════════════════════════════════════════════
# 3. 검색 의도 기반 추천
# ════════════════════════════════════════════════════════════════════
SQL_RECENT_KEYWORDS = """
SELECT  search_keyword, MAX(searched_at) AS last_at
FROM    search_history
WHERE   user_id = :user_id
GROUP BY search_keyword
ORDER BY last_at DESC
LIMIT   5
"""

# 검색어 → 전환율 높은 상품 (search_purchase_pattern)
SQL_SEARCH_INTENT = """
SELECT  p.product_id, p.product_name, p.brand, p.price, p.category_id,
        spp.conversion_rate, spp.search_keyword
FROM    search_purchase_pattern spp
JOIN    products p ON p.product_id = spp.product_id
WHERE   spp.search_keyword IN ({kw_list})
ORDER BY spp.conversion_rate DESC
LIMIT   :limit
"""


def recommend_search_intent(user_id: str) -> dict:
    db = get_db()
    kws = [k["search_keyword"] for k in db.query(SQL_RECENT_KEYWORDS, {"user_id": user_id})]
    # "OO 추천" 형태도 기본 키워드로 정규화해 포함
    norm = set()
    for k in kws:
        norm.add(k)
        norm.add(k.replace(" 추천", "").strip())
    if not norm:
        return _empty("검색 의도 기반 추천")

    kw_list = ", ".join(f"'{k}'" for k in norm)
    sql = SQL_SEARCH_INTENT.format(kw_list=kw_list)
    rows = db.query(sql, {"limit": config.REC_LIMIT})

    items = [{
        "product_id": r["product_id"], "name": r["product_name"],
        "brand": r["brand"], "price": _won(r["price"]),
        "category_id": r["category_id"],
        "tag": f"전환율 {round(r['conversion_rate'] * 100)}%",
    } for r in rows]

    sample_kw = next(iter(norm))
    return {
        "id": "search", "algo": "검색 의도 기반 추천",
        "title": "최근 검색하신 키워드 기반으로 골라드릴까요?",
        "desc": f'"{sample_kw}" 등 최근 검색 후 많이 산 상품이에요',
        "result_title": "최근 검색 키워드 기반 추천",
        "result_sub": "같은 키워드 검색 후 구매 전환율 높은 상품",
        "items": items,
    }


# ════════════════════════════════════════════════════════════════════
# 4. 소진/재구매 리마인드
# ════════════════════════════════════════════════════════════════════
SQL_REPURCHASE = """
SELECT  p.product_id, p.product_name, p.brand, p.price, p.category_id,
        cat.category_name, cat.avg_lifespan_days,
        COUNT(*) AS times, MAX(ph.purchased_at) AS last_at
FROM    purchase_history ph
JOIN    products   p   ON p.product_id   = ph.product_id
JOIN    categories cat ON cat.category_id = p.category_id
WHERE   ph.user_id = :user_id
GROUP BY p.product_id, p.product_name, p.brand, p.price, p.category_id,
         cat.category_name, cat.avg_lifespan_days
HAVING  COUNT(*) >= :repeat_min
ORDER BY times DESC, last_at DESC
LIMIT   :limit
"""


def recommend_repurchase(user_id: str) -> dict:
    db = get_db()
    rows = db.query(
        SQL_REPURCHASE,
        {"user_id": user_id, "repeat_min": config.REPEAT_PURCHASE_MIN,
         "limit": config.REC_LIMIT},
    )
    items = [{
        "product_id": r["product_id"], "name": r["product_name"],
        "brand": r["brand"], "price": _won(r["price"]),
        "category_id": r["category_id"],
        "tag": f"반복 구매 {r['times']}회 · 소진 {r['avg_lifespan_days']}일",
    } for r in rows]

    return {
        "id": "repurchase", "algo": "소진 리마인드",
        "title": "늘 쓰시던 거 슬슬 떨어질 때 됐어요",
        "desc": "반복 구매 주기 기준으로 지금이 재구매 타이밍이에요",
        "result_title": "재구매 타이밍 상품",
        "result_sub": "동일 상품 반복 구매 + 카테고리 소진 주기 기준",
        "items": items,
    }


def _empty(algo: str) -> dict:
    return {"id": "na", "algo": algo, "title": algo, "desc": "",
            "result_title": algo, "result_sub": "", "items": []}


# ── STEP3 옵션 구성 (클렌징 결과에 따라 노출, 화면설계서 S03) ──────────
ALL_RECOMMENDERS = {
    "cf": recommend_cf,
    "trend": recommend_trend,
    "search": recommend_search_intent,
    "repurchase": recommend_repurchase,
}


def get_recommendation_options(user_id: str, analysis: dict | None = None) -> list[dict]:
    """유저의 클렌징 결과에 맞는 추천 옵션 카드 목록을 반환한다.

    - 충동/시즌 정리 → 트렌드·검색의도·CF
    - 보관 상품 존재 → 소진/재구매
    항상 CF 는 포함, 보관 있으면 repurchase 포함.
    """
    from .bucket import get_cart_analysis
    if analysis is None:
        analysis = get_cart_analysis(user_id)

    type_count = analysis["type_count"]
    has_keep_storage = any(
        i["type"] == "보관" for i in analysis["keep_items"]
    )

    order = ["cf"]
    if type_count.get("충동") or type_count.get("시즌"):
        order.append("trend")
    order.append("search")
    if has_keep_storage:
        order.append("repurchase")

    # 중복 제거 + 순서 유지
    seen, picked = set(), []
    for key in order:
        if key not in seen:
            seen.add(key)
            picked.append(key)

    return [ALL_RECOMMENDERS[k](user_id) for k in picked]
