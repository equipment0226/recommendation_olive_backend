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
# 1. CF — 유사 고객 구매 기반 추천 (Gower 유사도 SQL)
# ════════════════════════════════════════════════════════════════════
# 유사 고객 선정을 **SQL 한 문장의 Gower 유사도**로 계산한다(파이썬 루프/동적 SQL 제거).
#   - skin_type    : 대칭형 범주 피처 → 같은 그룹만 후보(하드 필터, 그룹 내 유사도 1)
#   - skin_concerns: 비대칭형 다중값 범주 피처 → 공유 고민 수로 부분 유사도
# 공유 고민 계산은 파이썬 split 없이 MySQL 문자열·집합 함수로 수행한다.
#   ① 대상 고객의 고민 문자열을 JSON_TABLE 로 토큰 행으로 분해(`|` → JSON 배열)
#   ② 각 후보 고객의 고민 문자열(`|`→`,`)에 대해 FIND_IN_SET 으로 공유 토큰을 집계
# Gower 유사도(0~1) = (skin_type 일치 1 + 공유고민/전체고민)/2 를 SQL 에서 산출하며,
# 거리 = 1 - 유사도 로 정렬·필터에 쓸 수 있다(현재 정책은 공유고민>0 하드필터).
#
# 정책(동작 보존): "같은 피부타입 AND 공유 고민 ≥1" 이면 유사, 한 명도 없으면
# 같은 피부타입 전체로 폴백. 두 분기 모두 SQL 의 CASE/MAX 로 처리한다.
SQL_USER = "SELECT skin_type, skin_concerns, age_group FROM users WHERE user_id = :user_id"

# 유사 고객 선정 CTE — 두 쿼리(목록/상품)에서 공통으로 재사용한다.
# [성능 최적화]
#   ① raw_shared(FIND_IN_SET 공유 고민 수)를 peer_shared CTE 에서 후보당 단 1회만 계산.
#      기존엔 shared 와 gower 식에서 같은 FIND_IN_SET 을 2번 돌려 CPU 가 2배였다.
#   ② 전체 고민 수(total)도 tok_count CTE 로 1회만 집계해 CROSS JOIN 으로 상수처럼 사용.
#   ③ similar 의 WHERE 절 MAX(shared) 상관 서브쿼리 → sim_stat CTE(단일 로우) 로 분리해
#      CROSS JOIN. 행마다 peer_sim 전체를 재스캔하던 비용을 1회 스캔으로 축소.
_CTE_SIMILAR_USERS = """
WITH me AS (
    SELECT skin_type, skin_concerns
    FROM   users
    WHERE  user_id = :user_id
),
my_tokens AS (
    -- 대상 고객의 고민을 토큰 행으로 분해: '모공|여드름' → ["모공","여드름"]
    SELECT jt.tok
    FROM   me
    JOIN   JSON_TABLE(
               CONCAT('["', REPLACE(me.skin_concerns, '|', '","'), '"]'),
               '$[*]' COLUMNS (tok VARCHAR(255) PATH '$')
           ) AS jt
),
tok_count AS (
    -- 전체 고민 수(분모)를 1회만 집계 → 후보마다 재계산하지 않는다
    SELECT COUNT(*) AS total FROM my_tokens
),
peer_shared AS (
    -- 같은 피부타입 후보별 공유 고민 수(shared)를 FIND_IN_SET 으로 단 1회만 계산
    -- FIND_IN_SET 양변의 collation 을 명시적으로 맞춘다(JSON_TABLE 산출 토큰 vs 컬럼).
    SELECT u.user_id,
           (SELECT COUNT(*) FROM my_tokens t
            WHERE FIND_IN_SET(t.tok COLLATE utf8mb4_unicode_ci,
                              REPLACE(u.skin_concerns, '|', ',')) > 0) AS shared
    FROM   users u, me
    WHERE  u.skin_type = me.skin_type AND u.user_id <> :user_id
),
peer_sim AS (
    -- 위에서 1회 계산한 shared 를 재사용해 Gower 유사도(0~1) 산출
    -- gower = (skin_type 일치 1 + 공유고민/전체고민) / 2  ← 공식 동일
    SELECT ps.user_id, ps.shared,
           (1 + ps.shared / tc.total) / 2.0 AS gower
    FROM   peer_shared ps CROSS JOIN tok_count tc
),
sim_stat AS (
    -- 폴백 판정용 MAX(shared) 를 단일 로우 상수로 분리(반복 스캔 제거)
    SELECT MAX(shared) AS max_shared FROM peer_shared
),
similar AS (
    -- 공유 고민>0 후보, 없으면 같은 피부타입 전체로 폴백
    SELECT ps.user_id, ps.shared, ps.gower
    FROM   peer_sim ps CROSS JOIN sim_stat s
    WHERE  CASE WHEN s.max_shared > 0 THEN ps.shared > 0 ELSE 1 END
)
"""

# 유사 고객 목록 (peer_n 산출용)
SQL_CF_SIMILAR = _CTE_SIMILAR_USERS + "SELECT user_id FROM similar"

# 유사 고객들이 구매한 상품 랭킹 (이미 산 상품 제외)
# [성능 최적화] 이미 구매한 상품 제외를 NOT IN → NOT EXISTS 로 변경.
#   - NOT IN 은 서브쿼리에 NULL 이 섞이면 결과가 통째로 비는 위험이 있고, 옵티마이저가
#     세미조인/인덱스를 활용하기 어렵다. NOT EXISTS 는 (user_id, product_id) 인덱스를
#     타고 첫 매칭에서 단락 평가되어 안전하고 빠르다.
SQL_CF_PRODUCTS = _CTE_SIMILAR_USERS + """
SELECT  p.product_id, p.product_name, p.brand, p.price, p.category_id, cat.category_name,
        COUNT(*) AS buyers
FROM    purchase_history ph
JOIN    products   p   ON p.product_id   = ph.product_id
JOIN    categories cat ON cat.category_id = p.category_id
WHERE   ph.user_id IN (SELECT user_id FROM similar)
  AND   NOT EXISTS (
            SELECT 1 FROM purchase_history pe
            WHERE  pe.user_id = :user_id
              AND  pe.product_id = ph.product_id
        )
GROUP BY p.product_id, p.product_name, p.brand, p.price, p.category_id, cat.category_name
-- buyers·price 동률 시 plan 에 따라 순서가 흔들리지 않도록 product_id 로 결정적 정렬
ORDER BY buyers DESC, p.price DESC, p.product_id ASC
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
    similar = [r["user_id"] for r in db.query(SQL_CF_SIMILAR, {"user_id": user_id})]
    if not similar:
        return _empty("유사 고객 구매 기반")

    rows = db.query(SQL_CF_PRODUCTS, {"user_id": user_id, "limit": config.REC_LIMIT})

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

# 최근 달 상승 성분(trend_delta 상위 8) × 성분 포함 상품(전환율 상위 2)을
# **윈도우 함수 한 문장**으로 매칭·중복제거·정렬한다(파이썬 루프 제거).
#   - rising      : 상승 성분에 trend_delta 내림차순 순위(ing_rank)
#   - top_ing     : LIKE 조인 전에 상위 8개 성분으로 모수 선축소(아래 [성능 최적화])
#   - cand        : 성분별 포함 상품에 conversion_rate 내림차순 순위(conv_rank)
#   - top2        : 성분별 전환율 상위 2개만
#   - first_occ   : 동일 상품의 최초 등장(성분순위→전환순위)만 남겨 중복 제거
# 최종 정렬 = (ing_rank, conv_rank) → 파이썬 루프의 삽입 순서와 동일, 상위 :limit.
# [성능 최적화] 기존엔 모든 상승 성분에 대해 products LIKE '%성분%' 풀스캔 조인을 먼저
#   수행한 뒤 ing_rank<=8 로 잘라내, 불필요한 LIKE 조인·정렬(ROW_NUMBER) 부하가 컸다.
#   상위 8개 성분으로 먼저 모수를 줄인(top_ing) 다음에야 LIKE 조인을 수행하도록 순서를
#   바꿔, 가장 무거운 LIKE 조인과 PARTITION 정렬의 입력 카디널리티를 최소화했다.
SQL_TREND_PRODUCTS = """
WITH rising AS (
    SELECT ingredient, trend_delta,
           ROW_NUMBER() OVER (ORDER BY trend_delta DESC) AS ing_rank
    FROM   ingredient_trends
    WHERE  month = :month AND trend_delta > 0
),
top_ing AS (
    -- LIKE 조인 전에 상위 8개 성분만 남겨 조인·정렬 대상 모수를 선제 축소
    SELECT ingredient, trend_delta, ing_rank
    FROM   rising
    WHERE  ing_rank <= 8
),
conv AS (
    SELECT product_id, MAX(conversion_rate) AS conv
    FROM   search_purchase_pattern
    GROUP BY product_id
),
cand AS (
    SELECT r.ingredient, r.ing_rank, r.trend_delta,
           p.product_id, p.product_name, p.brand, p.price, p.category_id,
           ROW_NUMBER() OVER (PARTITION BY r.ingredient
                              ORDER BY c.conv DESC) AS conv_rank
    FROM   top_ing r
    JOIN   products p ON p.key_ingredients LIKE CONCAT('%', r.ingredient, '%')
    LEFT JOIN conv c ON c.product_id = p.product_id
),
top2 AS (
    SELECT * FROM cand WHERE conv_rank <= 2
),
first_occ AS (
    SELECT t.*,
           ROW_NUMBER() OVER (PARTITION BY product_id
                              ORDER BY ing_rank, conv_rank) AS occ
    FROM   top2 t
)
SELECT product_id, product_name, brand, price, category_id, ingredient, trend_delta
FROM   first_occ
WHERE  occ = 1
ORDER BY ing_rank, conv_rank
LIMIT :limit
"""


def recommend_trend(user_id: str) -> dict:
    db = get_db()
    month = db.query(SQL_LATEST_MONTH)[0]["m"]
    rows = db.query(SQL_TREND_PRODUCTS, {"month": month, "limit": config.REC_LIMIT})

    items = []
    for r in rows:
        delta = round(r["trend_delta"] * 100)
        items.append({
            "product_id": r["product_id"], "name": r["product_name"],
            "brand": r["brand"], "price": _won(r["price"]),
            "category_id": r["category_id"],
            "tag": f"{r['ingredient']} ↑{delta}%",
        })

    return {
        "id": "trend", "algo": "트렌드 기반 추천",
        "title": "요즘 뜨는 성분 트렌드 알려줄까요?",
        "desc": f"{month} 검색량이 급상승한 성분 기반이에요",
        "result_title": "검색량 상승 성분 트렌드 상품",
        "result_sub": f"{month} 기준 trend_delta 상위 성분 포함",
        "items": items,
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

# 최근 검색어 정규화("OO 추천"→"OO")와 전환율 상위 상품 매칭을 **SQL 한 문장**으로 처리.
#   - recent : 최근 검색어 상위 5개
#   - norm   : 원문 + ("OO 추천"의 접미사 제거·TRIM) 을 UNION 으로 집합화(중복 제거)
SQL_SEARCH_INTENT = """
WITH recent AS (
    SELECT search_keyword, MAX(searched_at) AS last_at
    FROM   search_history
    WHERE  user_id = :user_id
    GROUP BY search_keyword
    ORDER BY last_at DESC
    LIMIT  5
),
norm AS (
    SELECT search_keyword AS kw FROM recent
    UNION
    SELECT TRIM(REPLACE(search_keyword, ' 추천', '')) FROM recent
)
SELECT  p.product_id, p.product_name, p.brand, p.price, p.category_id,
        spp.conversion_rate, spp.search_keyword
FROM    search_purchase_pattern spp
JOIN    products p ON p.product_id = spp.product_id
WHERE   spp.search_keyword IN (SELECT kw FROM norm)
ORDER BY spp.conversion_rate DESC
LIMIT   :limit
"""


def recommend_search_intent(user_id: str) -> dict:
    db = get_db()
    kws = [k["search_keyword"] for k in db.query(SQL_RECENT_KEYWORDS, {"user_id": user_id})]
    if not kws:
        return _empty("검색 의도 기반 추천")

    rows = db.query(SQL_SEARCH_INTENT, {"user_id": user_id, "limit": config.REC_LIMIT})

    items = [{
        "product_id": r["product_id"], "name": r["product_name"],
        "brand": r["brand"], "price": _won(r["price"]),
        "category_id": r["category_id"],
        "tag": f"전환율 {round(r['conversion_rate'] * 100)}%",
    } for r in rows]

    # 표시용 대표 키워드(정규화 집합) — 노출 문구에만 사용
    norm = set()
    for k in kws:
        norm.add(k)
        norm.add(k.replace(" 추천", "").strip())
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
