"""CF 추천 동작 보존 검증.

리팩토링 전(파이썬 교집합 루프) vs 후(SQL Gower 유사도) 의 `recommend_cf`
결과가 모든 유저에 대해 완전히 동일한지 비교한다.

  python -m pytest test_cf_parity.py        # 또는
  python test_cf_parity.py
"""
import os

os.environ.setdefault("DATA_BACKEND", "csv")  # 인메모리 SQLite 로 검증

import config
from recommendation.db import get_db
from recommendation import recommender as R

# ── 리팩토링 이전(old) recommend_cf 재현 ────────────────────────────
SQL_SAME_SKIN_USERS_OLD = """
SELECT user_id, skin_concerns
FROM   users
WHERE  skin_type = :skin_type AND user_id <> :user_id
"""


def recommend_cf_old(user_id: str) -> dict:
    db = get_db()
    urows = db.query(R.SQL_USER, {"user_id": user_id})
    if not urows:
        return R._empty("유사 고객 구매 기반")
    me = urows[0]
    my_concerns = set((me["skin_concerns"] or "").split("|"))

    peers = db.query(
        SQL_SAME_SKIN_USERS_OLD,
        {"skin_type": me["skin_type"], "user_id": user_id},
    )
    similar = [
        p["user_id"] for p in peers
        if my_concerns & set((p["skin_concerns"] or "").split("|"))
    ]
    if not similar:
        similar = [p["user_id"] for p in peers]
    if not similar:
        return R._empty("유사 고객 구매 기반")

    user_list = ", ".join(f"'{u}'" for u in similar)
    sql = R.SQL_CF_PRODUCTS.format(user_list=user_list)
    rows = db.query(sql, {"user_id": user_id, "limit": config.REC_LIMIT})

    peer_n = len(similar)
    items = []
    for r in rows:
        rate = round(100 * r["buyers"] / peer_n)
        items.append({
            "product_id": r["product_id"], "name": r["product_name"],
            "brand": r["brand"], "price": R._won(r["price"]),
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


def _all_user_ids() -> list[str]:
    db = get_db()
    return [r["user_id"] for r in db.query("SELECT user_id FROM users ORDER BY user_id")]


def test_cf_parity():
    user_ids = _all_user_ids()
    mismatches = []
    for uid in user_ids:
        old = recommend_cf_old(uid)
        new = R.recommend_cf(uid)
        if old != new:
            mismatches.append((uid, old, new))
    assert not mismatches, f"{len(mismatches)}건 불일치: {[m[0] for m in mismatches]}"


if __name__ == "__main__":
    ids = _all_user_ids()
    diffs = 0
    for uid in ids:
        old = recommend_cf_old(uid)
        new = R.recommend_cf(uid)
        if old != new:
            diffs += 1
            print(f"[MISMATCH] {uid}")
            print(f"  old items={[i['product_id'] for i in old['items']]} sub={old['result_sub']!r}")
            print(f"  new items={[i['product_id'] for i in new['items']]} sub={new['result_sub']!r}")
    print("-" * 60)
    print(f"검사 유저 {len(ids)}명 · 불일치 {diffs}건 · "
          f"{'전부 동일 ✅' if diffs == 0 else '차이 있음 ❌'}")
