# 올리브영 장바구니 클렌징 추천 시스템 — SQL 쿼리 문서 II (구현본)

> **이 문서는 "왜 이 쿼리를 만들었는가"** 를 중심으로, 각 SQL이 **어떤 추천 기능·알고리즘**을
> 구현하는지 발표용으로 정리한 것이다. 각 항목은 **① 무엇을 위한 기능 → ② 어떻게 판단하나
> (알고리즘) → ③ 쿼리** 순으로 읽으면 된다. 성능 튜닝 등 깊은 기술 메모는 각 절 끝의
> *"⚙️ 구현 메모"* 로 접어 두었다.

## 한눈에 보기 — 6개 SQL이 만드는 사용자 경험

| 단계 | 기능 | 한 줄 설명 | 핵심 SQL |
|---|---|---|---|
| STEP1·2 | **방치 장바구니 자동 분류** | 오래 담아둔 상품을 5개 버킷(충동/시즌/니즈해결/고민/보관)으로 자동 정리 | `SQL_STALE_CART` |
| STEP3 ① | **유사 고객 추천 (CF)** | "나와 피부타입·고민이 비슷한 고객이 산 것"을 Gower 유사도로 추천 | `SQL_CF_PRODUCTS` |
| STEP3 ② | **트렌드 성분 추천** | "요즘 검색 급상승 성분"이 든 상품을 전환율 순으로 추천 | `SQL_TREND_PRODUCTS` |
| STEP3 ③ | **검색 의도 추천** | "최근 검색한 키워드"로 구매 전환 잘 되는 상품을 추천 | `SQL_SEARCH_INTENT` |
| STEP3 ④ | **소진·재구매 리마인드** | "반복 구매하던 상품"의 재구매 타이밍을 알림 | `SQL_REPURCHASE` |
| 공통 | **현재 계절 판정** | 오늘 날짜로 시즌을 정해 시즌 버킷 판단에 사용 | `SQL_SEASON_NOW` |

> - 기준 엔진: **MySQL 9.4**. `JSON_TABLE`·윈도우 함수·CTE 사용.
> - 설계 원형: [oliveyoung_SQL_쿼리문서.md](oliveyoung_SQL_쿼리문서.md) (이하 **문서 I**).
> - 구현 원칙: **분류·추천 로직은 전부 SQL** 이 수행하고, 파이썬은 화면 표시(₩·% 포맷,
>   문구)와 검증만 담당한다 → "로직은 DB에, 표현은 앱에".
> - 4절에 **문서 I 대비 무엇을·왜 바꿨는지**를 정리한다.

---

## 목차

1. [STEP1·2 — 방치 장바구니 자동 분류](#1-step12--방치-장바구니-자동-분류)
   - 1.1 현재 계절 판정 (`SQL_SEASON_NOW`)
   - 1.2 방치 장바구니 + 버킷 분류 (`SQL_STALE_CART`)
   - 1.3 검증용 유저 목록 (`SQL_ALL_STALE_USERS`)
2. [STEP3 — 4가지 추천 알고리즘](#2-step3--4가지-추천-알고리즘)
   - 2.1 유사 고객 추천 / Gower 유사도 (`SQL_CF_PRODUCTS`)
   - 2.2 트렌드 성분 추천 (`SQL_TREND_PRODUCTS`)
   - 2.3 검색 의도 추천 (`SQL_SEARCH_INTENT`)
   - 2.4 소진·재구매 리마인드 (`SQL_REPURCHASE`)
3. [인덱스 설계 — 빠른 응답을 위한 준비](#3-인덱스-설계--빠른-응답을-위한-준비)
4. [문서 I 대비 무엇을·왜 바꿨나](#4-문서-i-대비-무엇을왜-바꿨나)

> 쿼리의 `:name` 은 입력 파라미터다. 실행 시 [db.py](../recommendation/db.py) 가 MySQL 드라이버
> 형식으로 자동 변환한다.

---

## 1. STEP1·2 — 방치 장바구니 자동 분류

### 1.1 현재 계절 판정 — `SQL_SEASON_NOW`

**무엇을 위한 기능?**
시즌 상품(예: 여름용 선크림)이 "지금 계절에 맞는지"를 판단하려면 **오늘이 무슨 계절인지**를
알아야 한다. 이 조각이 그 기준 계절을 만들어 1.2의 시즌 버킷 판정에 쓰인다.

**어떻게 판단하나**
오늘 **월(月)** 을 보고 봄/여름/가을/겨울로 매핑한다. 운영자가 특정 계절을 강제하고 싶으면
`:season` 값으로 덮어쓸 수 있다(데모·테스트용).

```sql
-- SQL_SEASON_NOW : 오늘 월 → 계절
CASE MONTH(NOW())
    WHEN 3 THEN 'spring' WHEN 4 THEN 'spring' WHEN 5 THEN 'spring'
    WHEN 6 THEN 'summer' WHEN 7 THEN 'summer' WHEN 8 THEN 'summer'
    WHEN 9 THEN 'fall'   WHEN 10 THEN 'fall'  WHEN 11 THEN 'fall'
    ELSE 'winter'
END

-- SQL_CURRENT_SEASON : 방치 상품이 없을 때 계절만 단독 조회(폴백)
SELECT COALESCE(:season, <SQL_SEASON_NOW>) AS season;
```

> ⚙️ **구현 메모** — 문서 I 2.6의 `season_now` 규칙을 그대로 채택. 날짜 판정을 파이썬이 아닌
> SQL이 하므로 앱·DB 어디서 호출해도 동일한 기준이 보장된다.

---

### 1.2 방치 장바구니 + 버킷 분류 — `SQL_STALE_CART`

**무엇을 위한 기능?**
오래 담아두고 안 산 상품(방치 장바구니)을 사용자가 정리하기 쉽도록 **5개 버킷으로 자동
분류**한다. "삭제 추천(충동/시즌/니즈해결)" vs "유지(고민/보관)" 로 묶어 STEP1·2 화면을 채운다.

| 버킷 | 의미 | 사용자에게 주는 가치 |
|---|---|---|
| **충동** | 검색 없이 담았고 이후 관심도 없음 | "정리해도 되는 상품" |
| **클렌징_시즌** | 지금 계절과 안 맞는 시즌 상품 | "다음 시즌에" |
| **클렌징_니즈해결** | 담은 뒤 같은 카테고리를 이미 구매 | "이미 해결됨" |
| **고민** | 클릭·재검색 등 관심 신호가 있음 | "조금 더 고민 중" |
| **보관** | 반복 구매하던 상품 | "곧 또 살 것" |

**어떻게 판단하나 (우선순위 결정트리)**
한 상품에 여러 신호가 겹칠 수 있으므로 **위에서부터 먼저 맞는 규칙으로 확정**한다.

```
1) 같은 상품을 N회 이상 반복 구매했다           → 보관
2) 담은 뒤 같은 카테고리 상품을 이미 샀다        → 클렌징_니즈해결
3) 지금 계절과 안 맞는 시즌 상품이다
        ├ 그래도 클릭한 적 있으면               → 고민
        └ 아니면                               → 클렌징_시즌
4) 클릭했거나, 카테고리명을 검색한 적 있다       → 고민
5) 위 어디에도 안 걸린다                        → 충동
```

각 신호(반복구매 횟수·카테고리 구매시점·클릭 여부·검색 매칭)는 **사용자별로 한 번만 모아 두고**
방치 상품 목록에 붙여 판단한다.

**쿼리**

```sql
WITH pc AS (                       -- ① 반복구매 횟수 (보관 신호)
    SELECT product_id, COUNT(*) AS cnt
    FROM   purchase_history
    WHERE  user_id = :user_id
    GROUP BY product_id
),
cat_purchase AS (                  -- ② 카테고리별 최근 구매시점 (니즈해결 신호)
    SELECT pp.category_id, MAX(ph.purchased_at) AS last_purchased
    FROM   purchase_history ph
    JOIN   products pp ON pp.product_id = ph.product_id
    WHERE  ph.user_id = :user_id
    GROUP BY pp.category_id
),
clk AS (                           -- ③ 클릭한 상품 (관심 신호)
    SELECT DISTINCT product_clicked AS product_id
    FROM   search_history
    WHERE  user_id = :user_id AND product_clicked IS NOT NULL
),
kw_cat AS (                        -- ④ 카테고리명을 검색한 카테고리 (관심 신호)
    SELECT DISTINCT cat2.category_id
    FROM   search_history sh
    JOIN   categories cat2
        ON sh.search_keyword LIKE CONCAT('%', cat2.category_name, '%')
    WHERE  sh.user_id = :user_id
)
SELECT  c.cart_id, c.user_id, c.product_id, c.added_at, c.days_in_cart,
        c.referrer, c.quantity, c.expected_bucket,
        p.product_name, p.brand, p.category_id, p.key_ingredients,
        p.suitable_season, p.texture, p.volume_ml, p.volume_unit, p.price,
        cat.category_name, cat.avg_lifespan_days,
        COALESCE(:season, <SQL_SEASON_NOW>) AS cur_season,
        CASE                                       -- 우선순위 결정트리
            WHEN COALESCE(pc.cnt, 0) >= :repeat_min
                THEN '보관'
            WHEN cat_purchase.last_purchased > c.added_at
                THEN '클렌징_니즈해결'
            WHEN p.suitable_season NOT IN ('all', COALESCE(:season, <SQL_SEASON_NOW>))
                THEN CASE
                        WHEN clk.product_id IS NOT NULL THEN '고민'
                        ELSE '클렌징_시즌'
                     END
            WHEN clk.product_id IS NOT NULL OR kw_cat.category_id IS NOT NULL
                THEN '고민'
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
  AND   c.days_in_cart >= :stale_days              -- 오래 방치된 것만
ORDER BY c.days_in_cart DESC;
```

> ⚙️ **구현 메모**
> - 초기엔 분류를 파이썬 결정트리로 했으나, **조회 한 번에 버킷까지 산출하는 단일 SQL** 로 통합.
> - 처음엔 CASE 안에서 `EXISTS`/`COUNT` 서브쿼리를 행마다 돌렸지만, **사용자별 1회 집계 CTE
>   4개(`pc`/`cat_purchase`/`clk`/`kw_cat`)** 로 빼고 `LEFT JOIN` 하여 반복 스캔을 제거(N+1 해소).
> - 우선순위(보관>니즈해결>시즌>고민>충동)는 정답 라벨과의 일치율을 최대화한 값(4.2 참조).

---

### 1.3 검증용 유저 목록 — `SQL_ALL_STALE_USERS`

**무엇을 위한 기능?**
분류 정확도를 검증(`validate_all()`)할 때, 방치 상품을 가진 **모든 유저**를 한 번에 뽑아 1.2
분류 결과를 정답 라벨(`expected_bucket`)과 대조하는 데 쓴다. 추천 화면 자체에는 쓰이지 않는다.

```sql
SELECT DISTINCT user_id
FROM   cart_items
WHERE  days_in_cart >= :stale_days
ORDER BY user_id;
```

---

## 2. STEP3 — 4가지 추천 알고리즘

> STEP3은 **서로 다른 관점의 추천 4종**을 카드로 제시한다. 각 카드는 독립 SQL이며, "왜 이
> 상품을 추천하는가"의 근거가 다르다 → 비슷한 고객(CF) · 요즘 뜨는 성분(트렌드) · 내가 찾던 것
> (검색) · 곧 떨어질 것(재구매).

### 2.1 유사 고객 추천 / Gower 유사도 — `SQL_CF_PRODUCTS`

**무엇을 위한 기능?**
**협업 필터링(CF)** — "나와 피부가 비슷한 고객들이 많이 산 상품"을 추천한다. 추천 카드에는
"비슷한 고객 N명 중 M명이 구매" 같은 근거가 함께 표시된다.

**어떻게 판단하나 (Gower 유사도)**
두 사람이 얼마나 비슷한지를 두 가지 피부 속성으로 잰다.

1. **피부 타입**(지성/건성 등) — 같아야만 후보로 인정(하드 필터).
2. **피부 고민**(모공·여드름 …, `|` 로 여러 개) — **겹치는 고민이 많을수록** 더 비슷.

겹치는 고민 수를 전체 고민 수로 나눠 **0~1 사이 유사도(Gower)** 로 환산하고, 겹치는 고민이
있는 고객만 "유사 고객"으로 본다(겹치는 고객이 아무도 없으면 같은 피부 타입 전체로 폴백).
그다음 유사 고객들이 산 상품을 **구매자 수 순**으로 모아, 내가 **이미 산 상품은 빼고** 추천한다.

```
1단계  나의 피부타입·고민 추출
2단계  같은 피부타입 고객만 후보로
3단계  후보별 "겹치는 고민 수" 계산 → Gower 유사도(0~1)
4단계  겹치는 고민>0 인 고객 = 유사 고객 (없으면 같은 타입 전체)
5단계  유사 고객 구매상품 집계 → 내가 안 산 것만 → 구매자 많은 순 추천
```

> **왜 Gower인가?** 원래 설계(문서 I)는 `JSON_OVERLAPS` 로 고민 겹침을 봤지만, 실제 데이터의
> `skin_concerns` 는 `모공|여드름` 같은 **텍스트**라 JSON 함수를 못 쓴다. 그래서 피부타입(범주형)
> 과 고민(다중값)을 함께 다루는 **Gower 유사도** 개념을 SQL로 직접 구현했다.

**쿼리**

```sql
-- SQL_USER : 대상 유저 속성
SELECT skin_type, skin_concerns, age_group FROM users WHERE user_id = :user_id;

-- _CTE_SIMILAR_USERS : 유사 고객 선정 CTE (아래 두 쿼리가 공유)
WITH me AS (
    SELECT skin_type, skin_concerns
    FROM   users
    WHERE  user_id = :user_id
),
my_tokens AS (
    SELECT jt.tok
    FROM   me
    JOIN   JSON_TABLE(
               CONCAT('["', REPLACE(me.skin_concerns, '|', '","'), '"]'),
               '$[*]' COLUMNS (tok VARCHAR(255) PATH '$')
           ) AS jt
),
tok_count AS (                     -- 전체 고민 수(분모) 1회 집계
    SELECT COUNT(*) AS total FROM my_tokens
),
peer_shared AS (                   -- 후보당 FIND_IN_SET 단 1회(shared)
    SELECT u.user_id,
           (SELECT COUNT(*) FROM my_tokens t
            WHERE FIND_IN_SET(t.tok COLLATE utf8mb4_unicode_ci,
                              REPLACE(u.skin_concerns, '|', ',')) > 0) AS shared
    FROM   users u, me
    WHERE  u.skin_type = me.skin_type AND u.user_id <> :user_id
),
peer_sim AS (                      -- shared 재사용 → Gower 유사도
    SELECT ps.user_id, ps.shared,
           (1 + ps.shared / tc.total) / 2.0 AS gower
    FROM   peer_shared ps CROSS JOIN tok_count tc
),
sim_stat AS (                      -- 폴백 판정용 MAX(shared) 단일 로우 상수
    SELECT MAX(shared) AS max_shared FROM peer_shared
),
similar AS (
    SELECT ps.user_id, ps.shared, ps.gower
    FROM   peer_sim ps CROSS JOIN sim_stat s
    WHERE  CASE WHEN s.max_shared > 0 THEN ps.shared > 0 ELSE 1 END
)

-- SQL_CF_SIMILAR : 유사 고객 목록 (위 CTE + 아래)
SELECT user_id FROM similar;

-- SQL_CF_PRODUCTS : 유사 고객 구매 상품 랭킹 (위 CTE + 아래)
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
ORDER BY buyers DESC, p.price DESC, p.product_id ASC
LIMIT :limit;
```

> ⚙️ **구현 메모**
> - 파이썬의 split·교집합 루프와 동적 SQL 조립을 없애고 `JSON_TABLE`+`FIND_IN_SET` 으로 집합
>   연산을 SQL 안에서 끝냈다.
> - 성능: 겹치는 고민 수(`FIND_IN_SET`)를 **후보당 1번만** 계산해 재사용하고, 분모·`MAX(shared)`
>   를 단일 로우 CTE로 만들어 `CROSS JOIN`(상수화) → 반복 재스캔 제거. 이미 산 상품 제외는
>   `NOT EXISTS`(NULL 안전·인덱스 활용). 동률 시 순서가 흔들리지 않도록 `product_id ASC` 로
>   결정적 정렬(비즈니스 우선순위 buyers→price는 동일).
> - `JSON_TABLE` 토큰엔 `COLLATE utf8mb4_unicode_ci` 를 붙여 collation 충돌을 막는다.

---

### 2.2 트렌드 성분 추천 — `SQL_TREND_PRODUCTS`

**무엇을 위한 기능?**
**요즘 뜨는 성분**(검색·관심이 급상승한 성분)이 들어간 상품을 추천한다. "지금 화제인 성분
○○ 함유" 같은 트렌드 카드를 만든다.

**어떻게 판단하나**
이번 달 기준으로 인기 상승폭(`trend_delta`)이 큰 **상위 8개 성분**을 고르고, 그 성분이 든
상품을 찾아, 성분별로 **구매 전환율이 높은 상품 2개씩**을 뽑는다. 같은 상품이 여러 성분에
걸리면 한 번만 노출한다. 최종 정렬은 **성분 인기 순 → 전환율 순**.

```
1단계  이번 달 상승 성분 중 trend_delta 큰 순 → 상위 8개 성분
2단계  그 성분이 든 상품 찾기 (성분명이 상품 성분목록에 포함)
3단계  성분별 전환율 높은 상품 2개씩
4단계  중복 상품 1번만 → 성분인기·전환율 순 추천
```

> **데이터 출처:** `ingredient_trends`(성분 인기 추이) + `search_purchase_pattern`(전환율).
> 기획서 5.2의 트렌드 컨셉을 구현한 것으로, 문서 I에는 없던 STEP3 신규 추천이다.

**쿼리**

```sql
-- SQL_LATEST_MONTH
SELECT MAX(month) AS m FROM ingredient_trends;

-- SQL_TREND_PRODUCTS
WITH rising AS (
    SELECT ingredient, trend_delta,
           ROW_NUMBER() OVER (ORDER BY trend_delta DESC) AS ing_rank
    FROM   ingredient_trends
    WHERE  month = :month AND trend_delta > 0
),
top_ing AS (                       -- LIKE 조인 전에 상위 8개 성분으로 모수 선축소
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
LIMIT :limit;
```

> ⚙️ **구현 메모**
> - 성분별 루프 + 파이썬 중복 제거를 윈도우 함수(`ROW_NUMBER`) **단일 쿼리**로 합쳤다(성분순위·
>   전환순위·상품 중복 제거 모두 SQL).
> - 성능: 무거운 `LIKE` 조인 **전에** `top_ing` 으로 상위 8개 성분을 먼저 추려 입력을 줄였다
>   (필터 푸시다운). 순위·중복 제거 규칙은 그대로라 결과는 동일.

---

### 2.3 검색 의도 추천 — `SQL_SEARCH_INTENT`

**무엇을 위한 기능?**
사용자가 **최근 검색한 키워드**를 근거로, 그 키워드에서 실제 구매로 잘 이어진(전환율 높은)
상품을 추천한다. "최근 '수분크림' 을 찾으셨네요" 류의 카드를 만든다.

**어떻게 판단하나**
최근 검색어 5개를 뽑고, "OO 추천" 처럼 붙은 군더더기 접미사를 떼어 **키워드를 정규화**한다.
그 키워드로 `search_purchase_pattern`(검색→구매 전환 데이터)을 조회해 **전환율 높은 순**으로
상품을 추천한다.

```
1단계  최근 검색어 5개 (최신순)
2단계  키워드 정규화: 원문 + "OO 추천"→"OO"
3단계  그 키워드의 전환율 높은 상품 순으로 추천
```

> **왜 단순화했나?** 문서 I은 부분일치(`LIKE`)+가중치 점수를 썼지만, 데모 데이터엔 점수용
> `concern_target` JSON이 없어 **정확일치(`IN`) + 전환율 정렬**로 간결화했다. 키워드 정규화는
> 파이썬에서 SQL(`UNION`+`TRIM(REPLACE())`)로 옮겨 한 문장 안에서 처리한다.

**쿼리**

```sql
-- SQL_RECENT_KEYWORDS : 최근 검색어(존재 확인·표시용)
SELECT  search_keyword, MAX(searched_at) AS last_at
FROM    search_history
WHERE   user_id = :user_id
GROUP BY search_keyword
ORDER BY last_at DESC
LIMIT   5;

-- SQL_SEARCH_INTENT : 정규화 키워드 → 전환율 상위 상품
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
LIMIT   :limit;
```

---

### 2.4 소진·재구매 리마인드 — `SQL_REPURCHASE`

**무엇을 위한 기능?**
**반복 구매하던 상품의 재구매 타이밍**을 알린다. "전에 ○번 사셨던 △△, 슬슬 떨어질 때예요"
같은 리마인드 카드를 만든다.

**어떻게 판단하나**
같은 상품을 **N회 이상 반복 구매**한 이력을 모아, 구매 횟수가 많고 최근 구매일이 가까운 순으로
추천한다. 카테고리의 평균 소진 주기(`avg_lifespan_days`)도 함께 배지로 보여준다.

```
반복 구매 N회 이상 상품 → 구매 많은 순 · 최근 구매 순 → 재구매 추천
```

> **간소화 이유:** 문서 I은 `용량 ÷ 일일사용량` 으로 정밀 소진일(D-7)까지 계산하지만, 파일럿
> 카드는 **반복 횟수 + 평균 소진 주기**만 노출하므로 그 수준까지만 집계한다(정밀 소진일은 확장 여지).

**쿼리**

```sql
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
LIMIT   :limit;
```

---

## 3. 인덱스 설계 — 빠른 응답을 위한 준비

위 쿼리들이 자주 거치는 **조회 경로**(유저별 장바구니·구매·검색, 성분·전환율 조인)를 빠르게
하려고, [upload_to_mysql.py](../upload_to_mysql.py) 의 `create_indexes()` 가 데이터 적재 직후
아래 인덱스를 만든다(이미 있으면 건너뜀 — MySQL은 `CREATE INDEX IF NOT EXISTS` 미지원).

```sql
-- cart_items  (idx_cart_user 는 ORDER BY days_in_cart DESC 를 그대로 타도록 내림차순 인덱스)
CREATE INDEX idx_cart_user            ON cart_items(user_id, days_in_cart DESC);
CREATE INDEX idx_cart_user_prod       ON cart_items(user_id, product_id);
-- purchase_history (보관·니즈해결·재구매 집계)
CREATE INDEX idx_pu_user_prod_date    ON purchase_history(user_id, product_id, purchased_at);
CREATE INDEX idx_pu_user_date         ON purchase_history(user_id, purchased_at);
-- search_history (관심신호 EXISTS·키워드)
CREATE INDEX idx_sh_user_date         ON search_history(user_id, searched_at);
CREATE INDEX idx_sh_user_clicked      ON search_history(user_id, product_clicked);
CREATE INDEX idx_sh_keyword           ON search_history(search_keyword);
-- search_purchase_pattern (검색의도·트렌드 conv)
CREATE INDEX idx_spp_kw_conv          ON search_purchase_pattern(search_keyword, conversion_rate);
CREATE INDEX idx_spp_product          ON search_purchase_pattern(product_id);
-- products / ingredient_trends
CREATE INDEX idx_prod_category        ON products(category_id);
CREATE INDEX idx_trend_month          ON ingredient_trends(month, trend_delta);
CREATE INDEX idx_trend_ing_month      ON ingredient_trends(ingredient, month);
```

> 문서 I 6장 대비 추가분: `idx_sh_user_clicked`(클릭 신호), `idx_spp_product`(상품 조인),
> `idx_prod_category`(카테고리 조인), `idx_trend_month`(상승 성분 조회). 나머지는 문서 I과 동일.
>
> ⚙️ **구현 메모** — `idx_cart_user` 는 `(user_id, days_in_cart DESC)` **내림차순 인덱스**(MySQL
> 8.0+)다. 1.2의 `ORDER BY days_in_cart DESC` 를 인덱스 순서대로 바로 읽어 별도 정렬(filesort)을
> 없앤다.

---

## 4. 문서 I 대비 무엇을·왜 바꿨나

> 모든 변경은 **알고리즘 컨셉을 유지**하되, ① 데모 데이터(CSV→MySQL) 스키마 현실, ②
> `expected_bucket` 정답값과의 일치율, ③ pymysql/MySQL 엔진 제약 때문에 불가피했던 것이다.

### 4.1 스키마 차이로 인한 변경 (불가피)

| 항목 | 문서 I | 구현(문서 II) | 사유 |
|---|---|---|---|
| `skin_concerns` / `key_ingredients` / `concern_target` | `JSON` 타입, `JSON_OVERLAPS`·`JSON_CONTAINS` | **`|` 구분 TEXT**, `JSON_TABLE`+`FIND_IN_SET` / `LIKE '%성분%'` | CSV 적재 시 JSON 타입 미사용(TEXT). 원본 데이터가 `모공|여드름` 형식 |
| CF 유사 유저 | `JSON_OVERLAPS(skin_concerns)` | Gower 유사도(공유 고민 수) SQL | 위와 동일. 대신 Gower로 **유사도 수치화**까지 확장 |
| 검색 추천 매칭 | `LIKE '%kw%'` 부분일치 + `relevance_score` 가중합 | 정확일치 `IN` + `conversion_rate` 정렬 | `concern_target` JSON 부재·데모 규모. 키워드 정규화는 `UNION`/`TRIM` 으로 보존 |
| `concern_target` 필터 | `JSON_OVERLAPS(concern_target, skin_concerns)` | 미적용 | 적재 데이터에 `concern_target` JSON 없음 |

### 4.2 정답값(expected_bucket) 정합을 위한 의도적 차이

문서 I 2.6의 통합 CASE와 **판정 규칙·우선순위가 다르다**. 아래는 `cart_items.expected_bucket`
대조 일치율을 최대화하며 검증된(repo 메모리 기록) 결정이다.

| 분기 | 문서 I | 구현(문서 II) | 사유 |
|---|---|---|---|
| 우선순위 | 니즈해결 > 시즌 > 보관 > 고민 > 충동 | **보관 > 니즈해결 > 시즌 > 고민 > 충동** | 정답값 일치율 최대 |
| 보관(반복구매) | `purchased_at < added_at` (담기 전만) | **시점 무관** `COUNT(*) >= 2` | 정답값 기준 담은 후 구매도 보관 신호 |
| 시즌 + 클릭 | 무조건 `클렌징_시즌` | 클릭(`product_clicked`) 있으면 **`고민`** | 관심신호가 있으면 보관 가치로 재분류 |
| 관심신호 시점 | 재검색 `searched_at >= added_at` | **시점 무관** 클릭/카테고리 검색 | 정답값 기준 시점 제한 없이 신호 인정 |
| 니즈해결 상품 | `product_id != cart.product_id` (다른 상품) | 같은 상품 제외 안 함 | 보관 우선순위가 위에서 해소하므로 결과 동일, 식 단순화 |

> 이 차이들은 정답 라벨과 더 잘 맞아 **검증으로 채택**된 것이며, 데모/실서비스 데이터 라벨링
> 정책이 바뀌면 문서 I 규칙으로 되돌릴 수 있다.

### 4.3 구현 방식 변경 (파이썬 → SQL, 동작 보존)

| 항목 | 변경 전(초기 파일럿) | 변경 후 | 비고 |
|---|---|---|---|
| 버킷 분류 | 파이썬 `classify_cart_item` 결정트리 + 보조 쿼리 3종 | `SQL_STALE_CART` 단일 CASE | 검증: 100명 전부 동일 |
| CF 유사 유저 | 파이썬 split·교집합 루프 + 동적 SQL 문자열 | `JSON_TABLE`+`FIND_IN_SET` CTE | IN 절도 서브쿼리화 |
| 트렌드 | 성분 8개 파이썬 루프 + `seen` set | `ROW_NUMBER` 윈도우 단일 쿼리 | 정렬·중복제거 SQL화 |
| 검색 정규화 | 파이썬 set(원문+접미사 제거) | `UNION`+`TRIM(REPLACE())` CTE | 표시용 키워드만 파이썬 잔존 |
| 시즌 판정 | `config` 고정 `'spring'` | `MONTH(NOW())` 동적(override 가능) | 문서 I 2.6 `season_now` 채택 |

### 4.4 엔진/드라이버 제약 대응

| 이슈 | 증상 | 대응 |
|---|---|---|
| collation 충돌 | `FIND_IN_SET` 에서 *Illegal mix of collations* | `JSON_TABLE` 토큰에 `COLLATE utf8mb4_unicode_ci` 명시 |
| pymysql `%` 포맷 | `LIKE CONCAT('%',…)` 에서 `not enough arguments for format string` | [db.py](../recommendation/db.py) 가 리터럴 `%` → `%%` 이스케이프 후 `:name`→`%(name)s` 변환 |
| `CREATE INDEX IF NOT EXISTS` 미지원 | 재실행 시 중복 인덱스 에러 | `information_schema.statistics` 로 존재 확인 후 생성 |

### 4.5 표시(파이썬)로 남긴 부분 — 의도적

분류·추천 **선정·정렬·집계는 모두 SQL** 이며, 파이썬에는 아래만 남겼다(검증 제외):
- `_won()` 가격 `₩` 포맷, 구매율/전환율 **표시용 반올림**(MySQL `ROUND` 의 half-away vs 파이썬
  banker's rounding 차이로 인한 회귀를 피하려 표시 단계에서만 수행),
- `_build_reason()` 사유 문구 조립(언어/카피 영역),
- 표시용 대표 키워드 1개 선택.

### 4.6 실서비스 성능 리팩토링 (비즈니스 로직 불변)

옵티마이저가 효율적 실행 계획을 세우도록 **쿼리 구조만** 개선했다. 분류 우선순위·Gower 공식·
트렌드 순위 규칙 등 **비즈니스 로직은 100% 동일**하며, 파리티 테스트로 동작 보존을 검증한다.

| # | 대상 | 변경 전 | 변경 후 | I/O·CPU 절감 효과 |
|---|---|---|---|---|
| 1 | CF `peer_sim` | `shared`·`gower` 식에서 `FIND_IN_SET` **2회** | `peer_shared` 에서 **1회** 계산 후 재사용 | 후보당 문자열 스캔 절반 (CPU↓) |
| 1 | CF `tok_count`·`sim_stat` | `WHERE (SELECT MAX(shared) FROM peer_sim)` 상관 서브쿼리 | 단일 로우 CTE + `CROSS JOIN` 상수화 | 행마다 `peer_sim` 재스캔 → 1회 스캔 (I/O↓) |
| 1 | CF 상품 제외 | `product_id NOT IN (서브쿼리)` | `NOT EXISTS` | NULL 안전 + 인덱스 세미조인·단락평가 |
| 1 | CF 정렬 | `buyers DESC, price DESC` (동률 비결정) | `+ product_id ASC` 전순서 | `LIMIT` 경계 결정성 확보(파리티 안정) |
| 2 | `SQL_STALE_CART` | CASE 안 `EXISTS`/`COUNT` 상관 서브쿼리(행별 반복) | 사용자 단위 1회 집계 CTE 4개 `LEFT JOIN` | N+1 반복 스캔 제거, Hash/NL Join 일괄 처리 |
| 3 | `SQL_TREND_PRODUCTS` | 전체 상승 성분 × products `LIKE` 조인 후 `ing_rank<=8` 절단 | `top_ing`(상위 8) 선추출 후 `LIKE` 조인 | LIKE 조인·윈도우 정렬 입력 모수 축소(필터 푸시다운) |
| 4 | `idx_cart_user` | `(user_id, days_in_cart)` | `(user_id, days_in_cart DESC)` | `ORDER BY ... DESC` filesort 제거 |

> 1·3 의 결과 집합은 규칙상 불변이고, 2 는 `EXISTS`→비교식/조인 환산으로 의미가 동일하다.
> 1-④ 의 `product_id` 타이브레이크는 **동률(buyers·price 동일) 항목 간 순서만** 결정적으로
> 정해 `LIMIT` 경계를 안정화한 것으로, 비즈니스 정렬 우선순위(buyers→price)는 그대로다.

---

### 부록 — 검증

[test_sql_parity.py](../test_sql_parity.py) 가 **리팩토링 이전 파이썬 로직 vs 현재 SQL** 결과를
전체 유저(100명)에 대해 비교한다(버킷 분류 + 추천 4종). 시즌 기준은 서버 `NOW()` 로 맞춘다.
CF 의 동률 타이브레이크(`product_id`)는 신·구 스냅샷 쿼리에 동일하게 적용해 파리티를 well-defined
하게 만든다.

```powershell
python test_sql_parity.py
```
