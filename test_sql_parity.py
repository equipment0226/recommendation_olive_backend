"""SQL-only 리팩토링 동작 보존 검증 (MySQL 기준).

리팩토링 전(파이썬 로직)과 후(SQL-only)의 결과가 완전히 동일한지
모든 유저에 대해 비교한다. 비교 대상:
  1) STEP1·2 버킷 분류 (bucket.classify_cart_item 결정트리)
  2) STEP3 추천 4종 (cf / trend / search / repurchase)

  python -m pytest test_sql_parity.py        # 또는
  python test_sql_parity.py

검증은 실서비스와 동일한 MySQL 백엔드에서 수행한다(MySQL 전용 SQL 사용).
"""
import os
import sys

os.environ["DATA_BACKEND"] = "mysql"  # 실서비스와 동일 엔진에서 검증
try:
    sys.stdout.reconfigure(encoding="utf-8")  # cp949 콘솔에서도 이모지 출력 가능
except Exception:
    pass

import config
from recommendation.db import get_db
from recommendation import recommender as R
from recommendation import bucket as B


def _cur_season() -> str:
    """현재 계절 — 신규 SQL 과 동일하게 **서버 NOW()** 기준으로 산출한다.

    override(config.CURRENT_SEASON)가 있으면 그 값, 없으면 DB 의 MONTH(NOW())
    매핑(B.SQL_CURRENT_SEASON)을 그대로 조회해 월 경계에서도 완전 일치시킨다.
    """
    return get_db().query(
        B.SQL_CURRENT_SEASON, {"season": config.CURRENT_SEASON or None}
    )[0]["season"]


CUR_SEASON = _cur_season()


def _won(price) -> str:
    return f"₩{int(price):,}" if price is not None else ""


def _empty(algo: str) -> dict:
    return {"id": "na", "algo": algo, "title": algo, "desc": "",
            "result_title": algo, "result_sub": "", "items": []}


# ════════════════════════════════════════════════════════════════════
# OLD 구현 (리팩토링 이전 파이썬 로직을 그대로 박제)
# ════════════════════════════════════════════════════════════════════

# ── 버킷 분류 (old) ──────────────────────────────────────────────────
OLD_SQL_STALE_CART = """
SELECT  c.cart_id, c.user_id, c.product_id, c.added_at, c.days_in_cart,
        c.referrer, c.quantity,
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
OLD_SQL_REPEAT_COUNT = """
SELECT product_id, COUNT(*) AS cnt FROM purchase_history
WHERE user_id = :user_id GROUP BY product_id
"""
OLD_SQL_USER_PURCHASES = """
SELECT ph.product_id, ph.purchased_at, p.category_id
FROM purchase_history ph JOIN products p ON p.product_id = ph.product_id
WHERE ph.user_id = :user_id
"""
OLD_SQL_USER_SEARCHES = """
SELECT search_keyword, product_clicked, searched_at
FROM search_history WHERE user_id = :user_id
"""


def old_classify(row, repeat_counts, purchases, searches) -> str:
    pid = row["product_id"]
    cat = row["category_id"]
    added = row["added_at"]
    repeat2 = repeat_counts.get(pid, 0) >= config.REPEAT_PURCHASE_MIN
    samecat_after = any(
        pr["category_id"] == cat and (pr["purchased_at"] or "") > (added or "")
        for pr in purchases
    )
    season_mis = row["suitable_season"] not in ("all", CUR_SEASON)
    cat_name = row["category_name"]
    catmatch = any(cat_name in (s["search_keyword"] or "") for s in searches)
    clicked_this = any(s["product_clicked"] == pid for s in searches)
    if repeat2:
        return "보관"
    if samecat_after:
        return "클렌징_니즈해결"
    if season_mis:
        return "고민" if clicked_this else "클렌징_시즌"
    if clicked_this or catmatch:
        return "고민"
    return "충동"


def old_buckets(user_id: str) -> list[tuple]:
    db = get_db()
    params = {"user_id": user_id, "stale_days": config.STALE_DAYS}
    cart_rows = db.query(OLD_SQL_STALE_CART, params)
    repeat_counts = {r["product_id"]: r["cnt"]
                     for r in db.query(OLD_SQL_REPEAT_COUNT, {"user_id": user_id})}
    purchases = db.query(OLD_SQL_USER_PURCHASES, {"user_id": user_id})
    searches = db.query(OLD_SQL_USER_SEARCHES, {"user_id": user_id})
    return [(row["cart_id"], old_classify(row, repeat_counts, purchases, searches))
            for row in cart_rows]


# ── CF (old) ─────────────────────────────────────────────────────────
OLD_SQL_USER = "SELECT skin_type, skin_concerns, age_group FROM users WHERE user_id = :user_id"
OLD_SQL_SAME_SKIN = """
SELECT user_id, skin_concerns FROM users
WHERE skin_type = :skin_type AND user_id <> :user_id
"""
OLD_SQL_CF_PRODUCTS = """
SELECT  p.product_id, p.product_name, p.brand, p.price, p.category_id, cat.category_name,
        COUNT(*) AS buyers
FROM    purchase_history ph
JOIN    products   p   ON p.product_id   = ph.product_id
JOIN    categories cat ON cat.category_id = p.category_id
WHERE   ph.user_id IN ({user_list})
  AND   ph.product_id NOT IN (
            SELECT product_id FROM purchase_history WHERE user_id = :user_id)
GROUP BY p.product_id, p.product_name, p.brand, p.price, p.category_id, cat.category_name
ORDER BY buyers DESC, p.price DESC, p.product_id ASC
LIMIT :limit
"""


def old_cf(user_id: str) -> dict:
    db = get_db()
    urows = db.query(OLD_SQL_USER, {"user_id": user_id})
    if not urows:
        return _empty("유사 고객 구매 기반")
    me = urows[0]
    my_concerns = set((me["skin_concerns"] or "").split("|"))
    peers = db.query(OLD_SQL_SAME_SKIN, {"skin_type": me["skin_type"], "user_id": user_id})
    similar = [p["user_id"] for p in peers
               if my_concerns & set((p["skin_concerns"] or "").split("|"))]
    if not similar:
        similar = [p["user_id"] for p in peers]
    if not similar:
        return _empty("유사 고객 구매 기반")
    user_list = ", ".join(f"'{u}'" for u in similar)
    sql = OLD_SQL_CF_PRODUCTS.format(user_list=user_list)
    rows = db.query(sql, {"user_id": user_id, "limit": config.REC_LIMIT})
    peer_n = len(similar)
    items = []
    for r in rows:
        rate = round(100 * r["buyers"] / peer_n)
        items.append({"product_id": r["product_id"], "name": r["product_name"],
                      "brand": r["brand"], "price": _won(r["price"]),
                      "category_id": r["category_id"], "tag": f"구매율 {rate}%"})
    return {
        "id": "cf", "algo": "Collaborative Filtering",
        "title": "나와 비슷한 고객이 결국 뭘 샀는지 알려줄까요?",
        "desc": f"같은 피부타입·고민 고객 {peer_n}명의 선택이에요",
        "result_title": "비슷한 피부 고민 고객의 선택",
        "result_sub": f"{me['skin_type']} · {', '.join(my_concerns)} {peer_n}명 기준",
        "items": items,
    }


# ── 트렌드 (old) ─────────────────────────────────────────────────────
OLD_SQL_LATEST_MONTH = "SELECT MAX(month) AS m FROM ingredient_trends"
OLD_SQL_TREND_INGREDIENTS = """
SELECT ingredient, search_volume, trend_delta FROM ingredient_trends
WHERE month = :month AND trend_delta > 0
ORDER BY trend_delta DESC LIMIT 8
"""


def old_trend(user_id: str) -> dict:
    db = get_db()
    month = db.query(OLD_SQL_LATEST_MONTH)[0]["m"]
    rising = db.query(OLD_SQL_TREND_INGREDIENTS, {"month": month})
    items = []
    seen = set()
    for ing in rising:
        rows = db.query(
            """
            SELECT p.product_id, p.product_name, p.brand, p.price, p.category_id,
                   MAX(spp.conversion_rate) AS conv
            FROM products p
            LEFT JOIN search_purchase_pattern spp ON spp.product_id = p.product_id
            WHERE p.key_ingredients LIKE :pat
            GROUP BY p.product_id, p.product_name, p.brand, p.price, p.category_id
            ORDER BY conv DESC LIMIT 2
            """,
            {"pat": f"%{ing['ingredient']}%"},
        )
        delta = round(ing["trend_delta"] * 100)
        for r in rows:
            if r["product_id"] in seen:
                continue
            seen.add(r["product_id"])
            items.append({"product_id": r["product_id"], "name": r["product_name"],
                          "brand": r["brand"], "price": _won(r["price"]),
                          "category_id": r["category_id"],
                          "tag": f"{ing['ingredient']} ↑{delta}%"})
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


# ── 검색 의도 (old) ──────────────────────────────────────────────────
OLD_SQL_RECENT_KEYWORDS = """
SELECT search_keyword, MAX(searched_at) AS last_at FROM search_history
WHERE user_id = :user_id GROUP BY search_keyword ORDER BY last_at DESC LIMIT 5
"""
OLD_SQL_SEARCH_INTENT = """
SELECT p.product_id, p.product_name, p.brand, p.price, p.category_id,
       spp.conversion_rate, spp.search_keyword
FROM search_purchase_pattern spp JOIN products p ON p.product_id = spp.product_id
WHERE spp.search_keyword IN ({kw_list})
ORDER BY spp.conversion_rate DESC LIMIT :limit
"""


def old_search(user_id: str) -> dict:
    db = get_db()
    kws = [k["search_keyword"] for k in db.query(OLD_SQL_RECENT_KEYWORDS, {"user_id": user_id})]
    norm = set()
    for k in kws:
        norm.add(k)
        norm.add(k.replace(" 추천", "").strip())
    if not norm:
        return _empty("검색 의도 기반 추천")
    kw_list = ", ".join(f"'{k}'" for k in norm)
    sql = OLD_SQL_SEARCH_INTENT.format(kw_list=kw_list)
    rows = db.query(sql, {"limit": config.REC_LIMIT})
    items = [{"product_id": r["product_id"], "name": r["product_name"],
              "brand": r["brand"], "price": _won(r["price"]),
              "category_id": r["category_id"],
              "tag": f"전환율 {round(r['conversion_rate'] * 100)}%"} for r in rows]
    sample_kw = next(iter(norm))
    return {
        "id": "search", "algo": "검색 의도 기반 추천",
        "title": "최근 검색하신 키워드 기반으로 골라드릴까요?",
        "desc": f'"{sample_kw}" 등 최근 검색 후 많이 산 상품이에요',
        "result_title": "최근 검색 키워드 기반 추천",
        "result_sub": "같은 키워드 검색 후 구매 전환율 높은 상품",
        "items": items,
    }


# ── 소진/재구매 (old) ────────────────────────────────────────────────
OLD_SQL_REPURCHASE = """
SELECT p.product_id, p.product_name, p.brand, p.price, p.category_id,
       cat.category_name, cat.avg_lifespan_days,
       COUNT(*) AS times, MAX(ph.purchased_at) AS last_at
FROM purchase_history ph
JOIN products p ON p.product_id = ph.product_id
JOIN categories cat ON cat.category_id = p.category_id
WHERE ph.user_id = :user_id
GROUP BY p.product_id, p.product_name, p.brand, p.price, p.category_id,
         cat.category_name, cat.avg_lifespan_days
HAVING COUNT(*) >= :repeat_min
ORDER BY times DESC, last_at DESC LIMIT :limit
"""


def old_repurchase(user_id: str) -> dict:
    db = get_db()
    rows = db.query(OLD_SQL_REPURCHASE,
                    {"user_id": user_id, "repeat_min": config.REPEAT_PURCHASE_MIN,
                     "limit": config.REC_LIMIT})
    items = [{"product_id": r["product_id"], "name": r["product_name"],
              "brand": r["brand"], "price": _won(r["price"]),
              "category_id": r["category_id"],
              "tag": f"반복 구매 {r['times']}회 · 소진 {r['avg_lifespan_days']}일"}
             for r in rows]
    return {
        "id": "repurchase", "algo": "소진 리마인드",
        "title": "늘 쓰시던 거 슬슬 떨어질 때 됐어요",
        "desc": "반복 구매 주기 기준으로 지금이 재구매 타이밍이에요",
        "result_title": "재구매 타이밍 상품",
        "result_sub": "동일 상품 반복 구매 + 카테고리 소진 주기 기준",
        "items": items,
    }


# ════════════════════════════════════════════════════════════════════
# 비교 하니스
# ════════════════════════════════════════════════════════════════════
def _all_user_ids() -> list[str]:
    db = get_db()
    return [r["user_id"] for r in db.query("SELECT user_id FROM users ORDER BY user_id")]


def _new_buckets(user_id: str) -> list[tuple]:
    analysis = B.get_cart_analysis(user_id)
    items = analysis["cleansing_items"] + analysis["keep_items"]
    # 표시 순서가 아닌 cart_id 매칭으로 비교 (그룹 분리 영향 제거)
    return sorted((i["cart_id"], i["bucket"]) for i in items)


def _check(name, old_fn, new_fn, ids):
    diffs = []
    for uid in ids:
        o, n = old_fn(uid), new_fn(uid)
        if o != n:
            diffs.append((uid, o, n))
    return diffs


def test_bucket_parity():
    ids = _all_user_ids()
    diffs = []
    for uid in ids:
        o = sorted(old_buckets(uid))
        n = _new_buckets(uid)
        if o != n:
            diffs.append((uid, o, n))
    assert not diffs, f"버킷 {len(diffs)}건 불일치: {[d[0] for d in diffs]}"


def test_cf_parity():
    diffs = _check("cf", old_cf, R.recommend_cf, _all_user_ids())
    assert not diffs, f"CF {len(diffs)}건 불일치: {[d[0] for d in diffs]}"


def test_trend_parity():
    diffs = _check("trend", old_trend, R.recommend_trend, _all_user_ids())
    assert not diffs, f"트렌드 {len(diffs)}건 불일치: {[d[0] for d in diffs]}"


def test_search_parity():
    diffs = _check("search", old_search, R.recommend_search_intent, _all_user_ids())
    assert not diffs, f"검색 {len(diffs)}건 불일치: {[d[0] for d in diffs]}"


def test_repurchase_parity():
    diffs = _check("repurchase", old_repurchase, R.recommend_repurchase, _all_user_ids())
    assert not diffs, f"재구매 {len(diffs)}건 불일치: {[d[0] for d in diffs]}"


if __name__ == "__main__":
    ids = _all_user_ids()
    print(f"검증 유저 {len(ids)}명 (MySQL)\n" + "-" * 60)

    checks = [
        ("버킷 분류", lambda u: sorted(old_buckets(u)), _new_buckets),
        ("CF", old_cf, R.recommend_cf),
        ("트렌드", old_trend, R.recommend_trend),
        ("검색 의도", old_search, R.recommend_search_intent),
        ("재구매", old_repurchase, R.recommend_repurchase),
    ]
    all_ok = True
    for name, ofn, nfn in checks:
        diffs = _check(name, ofn, nfn, ids)
        mark = "전부 동일 ✅" if not diffs else f"불일치 {len(diffs)}건 ❌"
        print(f"  {name:8s} : {mark}")
        if diffs:
            all_ok = False
            for uid, o, n in diffs[:3]:
                print(f"     [{uid}]\n       old={o}\n       new={n}")
    print("-" * 60)
    print("최종: 전부 동일 ✅" if all_ok else "최종: 차이 있음 ❌")
