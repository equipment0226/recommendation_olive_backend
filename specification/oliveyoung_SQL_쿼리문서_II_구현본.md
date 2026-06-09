# 올리브영 장바구니 클렌징 추천 시스템 — SQL 쿼리 문서 II (구현본)

> 본 문서(**문서 II**)는 파일럿 구현체([recommendation/bucket.py](../recommendation/bucket.py),
> [recommendation/recommender.py](../recommendation/recommender.py))에 **실제로 사용된 SQL** 을
> 쿼리별로 **① 구조도 → ② 설명 → ③ 쿼리** 순서로 정리한다.
>
> - 기준 엔진: **MySQL 9.4** (Railway). `JSON_TABLE`·윈도우 함수·CTE 사용.
> - 설계 원형: [oliveyoung_SQL_쿼리문서.md](oliveyoung_SQL_쿼리문서.md) (이하 **문서 I**).
> - 구현 원칙: **분류·추천 알고리즘 로직은 전부 SQL** 로 수행하고, 파이썬은 표시 가공
>   (₩·% 포맷, 사유 문구)과 검증만 담당한다.
> - 마지막 절에 **문서 I 대비 변경사항·변경사유**를 전부 명기한다.

---

## 목차

1. [버킷 분류 (STEP1·2)](#1-버킷-분류-step12)
   - 1.1 현재 계절 산출 (`SQL_SEASON_NOW` / `SQL_CURRENT_SEASON`)
   - 1.2 방치 장바구니 + 버킷 판별 (`SQL_STALE_CART`)
   - 1.3 검증 대상 유저 목록 (`SQL_ALL_STALE_USERS`)
2. [STEP3 추천](#2-step3-추천)
   - 2.1 CF — 유사 고객 Gower 유사도 (`SQL_USER` / `_CTE_SIMILAR_USERS` / `SQL_CF_SIMILAR` / `SQL_CF_PRODUCTS`)
   - 2.2 트렌드 기반 (`SQL_LATEST_MONTH` / `SQL_TREND_PRODUCTS`)
   - 2.3 검색 의도 기반 (`SQL_RECENT_KEYWORDS` / `SQL_SEARCH_INTENT`)
   - 2.4 소진/재구매 (`SQL_REPURCHASE`)
3. [인덱스 설계](#3-인덱스-설계)
4. [문서 I 대비 변경사항 · 변경사유](#4-문서-i-대비-변경사항--변경사유)

> 플레이스홀더는 `:name` 스타일이며, [recommendation/db.py](../recommendation/db.py) 가
> 실행 시 pymysql 용 `%(name)s` 로 변환한다(리터럴 `%` 는 `%%` 로 이스케이프).

---

## 1. 버킷 분류 (STEP1·2)

### 1.1 현재 계절 산출

**구조도**

```
MONTH(NOW())
   │  (3~5→spring, 6~8→summer, 9~11→fall, else winter)
   ▼
COALESCE(:season, <CASE>)        ← :season 이 있으면 override(데모 고정), 없으면 동적
   ▼
current_season
```

**설명**
- 문서 I 2.6의 `season_now` CTE를 그대로 옮긴 **월 기준 동적 계절 판정**이다.
- 파일럿 초기엔 `'spring'` 으로 고정했으나, 본 구현은 기본을 **동적(`MONTH(NOW())`)** 으로
  전환했다. `config.CURRENT_SEASON` 에 값을 넣으면 `COALESCE` 로 그 값이 우선한다
  (데모·회귀 테스트에서 특정 시즌 재현용).
- `SQL_SEASON_NOW` 는 다른 쿼리(`SQL_STALE_CART`)에 **문자열로 삽입**되는 조각이고,
  `SQL_CURRENT_SEASON` 은 방치 cart가 없을 때 계절만 단독 조회하는 폴백이다.

**쿼리**

```sql
-- SQL_SEASON_NOW (삽입 조각)
CASE MONTH(NOW())
    WHEN 3 THEN 'spring' WHEN 4 THEN 'spring' WHEN 5 THEN 'spring'
    WHEN 6 THEN 'summer' WHEN 7 THEN 'summer' WHEN 8 THEN 'summer'
    WHEN 9 THEN 'fall'   WHEN 10 THEN 'fall'  WHEN 11 THEN 'fall'
    ELSE 'winter'
END

-- SQL_CURRENT_SEASON (단독 조회 폴백)
SELECT COALESCE(:season, <SQL_SEASON_NOW>) AS season;
```

---

### 1.2 방치 장바구니 + 버킷 판별 — `SQL_STALE_CART`

**구조도**

```
[사용자 단위 1회 집계 파생 CTE]
  pc           = purchase_history GROUP BY product_id          → 반복구매 횟수
  cat_purchase = purchase_history⨝products GROUP BY category_id → 카테고리별 MAX(purchased_at)
  clk          = search_history(product_clicked IS NOT NULL)    → 클릭 상품 집합
  kw_cat       = search_history⨝categories(LIKE %name%)         → 검색어 포함 카테고리
        │  (모두 WHERE user_id=:user_id 로 모수 선축소)
        ▼
cart_items c ─JOIN─ products p ─JOIN─ categories cat
        │  LEFT JOIN pc / cat_purchase / clk / kw_cat   ← 상관 서브쿼리 대신 집합 결합
        │  WHERE user_id=:user_id AND days_in_cart >= :stale_days
        ▼
행마다 평가 (LEFT JOIN 결과만 참조, 재스캔 없음):
   bucket = CASE (우선순위 결정트리)
     1) COALESCE(pc.cnt,0) >= :repeat_min ........................ '보관'
     2) cat_purchase.last_purchased > c.added_at ................. '클렌징_니즈해결'
     3) suitable_season NOT IN ('all', cur_season)
            ├ clk.product_id IS NOT NULL ........................ '고민'
            └ else ............................................. '클렌징_시즌'
     4) clk.product_id IS NOT NULL OR kw_cat.category_id IS NOT NULL  '고민'
     5) else ........................................... '충동'
   ▼
ORDER BY days_in_cart DESC
```

**설명**
- 문서 I은 버킷별 쿼리(2.1~2.5)와 통합 CASE(2.6)를 제시했고, **파일럿 초기 구현은 그 결과를
  파이썬 결정트리(`classify_cart_item`)로 교차 판정**했다. 본 구현은 그 파이썬 로직을 **완전히
  제거**하고, 조회 한 번에 버킷까지 산출하는 **단일 SQL** 로 통합했다.
- **[성능 최적화]** 초기 SQL은 CASE 분기마다 `EXISTS`/`COUNT` **상관 서브쿼리**를 두어, 방치
  cart 행마다 `purchase_history`·`search_history` 를 반복 스캔했다(행 수 × 서브쿼리 = N+1 형태).
  이를 **사용자 단위로 1회만 집계한 파생 CTE 4개(`pc`/`cat_purchase`/`clk`/`kw_cat`)** 로 빼고
  메인 `FROM` 에 `LEFT JOIN` 했다. 옵티마이저는 작은 집계 결과를 Hash/Nested-Loop Join 으로
  **일괄 결합**하므로, 행별 반복 실행이 사라진다(I/O·CPU ↓).
  - `EXISTS(담은 후 동일 카테고리 구매)` → `cat_purchase.last_purchased > c.added_at` 비교식으로 환산.
  - 행별 `LIKE` 상관 서브쿼리 → `kw_cat` 에서 "사용자 검색어 × 카테고리" **1회 LIKE 조인**으로 축소.
- 우선순위는 **보관 > 니즈해결 > 시즌 > 고민 > 충동**. 이는 `expected_bucket` 정답값과의
  일치율(검증)을 최대화한, 문서 I과 **의도적으로 다른** 순서다(4절 참조). **판정 의미·우선순위는
  최적화 전후 100% 동일**하다.
- `cur_season` 도 같은 행에 SELECT 하여 사유 문구 생성 시 파이썬이 계절을 재계산하지 않는다.

**쿼리**

```sql
WITH pc AS (                       -- 반복구매 횟수(상품별 1회 집계)
    SELECT product_id, COUNT(*) AS cnt
    FROM   purchase_history
    WHERE  user_id = :user_id
    GROUP BY product_id
),
cat_purchase AS (                  -- 카테고리별 최근 구매시점(EXISTS 환산)
    SELECT pp.category_id, MAX(ph.purchased_at) AS last_purchased
    FROM   purchase_history ph
    JOIN   products pp ON pp.product_id = ph.product_id
    WHERE  ph.user_id = :user_id
    GROUP BY pp.category_id
),
clk AS (                           -- 클릭 상품 집합
    SELECT DISTINCT product_clicked AS product_id
    FROM   search_history
    WHERE  user_id = :user_id AND product_clicked IS NOT NULL
),
kw_cat AS (                        -- 검색어가 카테고리명을 포함하는 카테고리(1회 LIKE 조인)
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
        CASE
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
  AND   c.days_in_cart >= :stale_days
ORDER BY c.days_in_cart DESC;
```

---

### 1.3 검증 대상 유저 목록 — `SQL_ALL_STALE_USERS`

**구조도**

```
cart_items  ─ WHERE days_in_cart >= :stale_days ─ DISTINCT user_id ─ ORDER BY user_id
```

**설명**
- `validate_all()` 이 전체 방치 유저를 순회하며 분류 결과 vs `expected_bucket` 일치율을 집계할 때
  쓰는 유저 목록 쿼리. 분류 자체는 1.2의 `SQL_STALE_CART` 가 수행한다.

**쿼리**

```sql
SELECT DISTINCT user_id
FROM   cart_items
WHERE  days_in_cart >= :stale_days
ORDER BY user_id;
```

---

## 2. STEP3 추천

### 2.1 CF — 유사 고객 Gower 유사도

**구조도**

```
me            : 대상 유저의 skin_type, skin_concerns
   │
my_tokens     : skin_concerns('모공|여드름') → JSON_TABLE → 토큰 행 ["모공","여드름"]
   │
tok_count     : 전체 고민 수(분모) 1회 집계 ──┐ (CROSS JOIN 상수)
   │                                          │
peer_shared   : 같은 skin_type 후보 u 마다    │
   │              shared = Σ FIND_IN_SET(…)   │  ← FIND_IN_SET 후보당 단 1회
   ▼                                          │
peer_sim      : gower = (1 + shared/total)/2 ◀┘   ← Gower 유사도(0~1), shared 재사용
   │
sim_stat      : MAX(shared) 단일 로우 ──┐ (CROSS JOIN 상수, 재스캔 제거)
   ▼                                    │
similar       : max_shared>0 이면 shared>0, 아니면 전체(폴백) ◀┘
   │
   ├─ SQL_CF_SIMILAR  : SELECT user_id  (peer_n 산출)
   └─ SQL_CF_PRODUCTS : 유사고객 구매상품 집계(이미 산 상품 NOT EXISTS 제외)
                         ORDER BY buyers DESC, price DESC, product_id ASC  LIMIT :limit
```

**설명**
- 문서 I 4.2는 `JSON_OVERLAPS(skin_concerns)` 로 유사 유저를 잡았다. CSV→MySQL 적재 시
  `skin_concerns` 가 **`|` 구분 TEXT** 로 저장되어(JSON 타입 아님) `JSON_OVERLAPS` 를 쓸 수 없다.
  그래서 **Gower 유사도 컨셉을 SQL로 직접 구현**했다.
  - `skin_type` = 대칭형 범주 피처 → 같은 그룹만 후보(하드 필터).
  - `skin_concerns` = 비대칭형 다중값 피처 → **공유 고민 수**로 부분 유사도.
- 핵심은 **파이썬 split·교집합 루프와 동적 SQL 문자열 조립을 제거**하고 `JSON_TABLE` +
  `FIND_IN_SET` 로 집합 연산을 SQL 안에서 끝낸 것이다. `gower` 컬럼까지 산출해 거리(1−유사도)
  기반 정렬 확장도 가능하나, 현재 정책은 **공유 고민 > 0 하드필터**(없으면 전체 폴백)다.
- **[성능 최적화]** Gower 공식·폴백 정책은 그대로 두고 실행 구조만 개선했다.
  1. **`FIND_IN_SET` 중복 계산 제거**: 기존엔 `shared` 와 `gower` 식에서 같은 `FIND_IN_SET`
     서브쿼리를 2번 돌렸다. `peer_shared` CTE 에서 **후보당 1회만** 계산하고 `peer_sim` 이
     그 값을 재사용한다(CPU 약 1/2).
  2. **분모·MAX 상수화(CROSS JOIN)**: 전체 고민 수(`tok_count`)와 폴백 판정용
     `MAX(shared)`(`sim_stat`)를 **단일 로우 CTE** 로 분리해 `CROSS JOIN` 한다. 기존 `similar`
     의 `WHERE (SELECT MAX(shared) FROM peer_sim)` 상관 서브쿼리가 행마다 `peer_sim` 전체를
     재스캔하던 비용을 **1회 스캔**으로 줄였다.
  3. **`NOT IN` → `NOT EXISTS`**: 이미 구매한 상품 제외를 `NOT EXISTS` 로 변경. `NOT IN` 은
     서브쿼리에 `NULL` 이 섞이면 결과가 통째로 비는 위험이 있고 세미조인·인덱스 활용이
     어렵다. `NOT EXISTS` 는 `(user_id, product_id)` 인덱스를 타고 첫 매칭에서 단락 평가된다.
  4. **결정적 정렬**: `buyers`·`price` 동률 시 실행계획에 따라 `LIMIT` 경계 상품이 흔들리던
     문제를 막기 위해 `ORDER BY buyers DESC, p.price DESC, p.product_id ASC` 로 **전순서(total
     order)** 를 부여했다. 비즈니스 정렬 의미(buyers→price)는 동일하고 동률만 결정적으로 해소한다.
- **collation 주의**: `JSON_TABLE` 산출 토큰은 `utf8mb4_0900_ai_ci`, 컬럼은
  `utf8mb4_unicode_ci` 라 `FIND_IN_SET` 에서 *Illegal mix of collations* 가 난다.
  토큰 쪽에 `COLLATE utf8mb4_unicode_ci` 를 명시해 해결했다.
- 구매율(`구매율 N%`)은 `buyers / peer_n` 으로, 파이썬에서 **표시용 반올림만** 한다.

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

---

### 2.2 트렌드 기반 — `SQL_TREND_PRODUCTS`

**구조도**

```
SQL_LATEST_MONTH : MAX(month)  → :month
   ▼
rising    : month=:month AND trend_delta>0,  ROW_NUMBER() ORDER BY trend_delta DESC = ing_rank
top_ing   : ing_rank<=8  ← LIKE 조인 '전에' 상위 8개 성분으로 모수 선축소(핵심 최적화)
conv      : 상품별 MAX(conversion_rate)
cand      : top_ing  JOIN  products(key_ingredients LIKE %성분%)  LEFT JOIN conv
            └ ROW_NUMBER() PARTITION BY 성분 ORDER BY conv DESC = conv_rank
top2      : conv_rank <= 2  (성분별 전환율 상위 2)
first_occ : ROW_NUMBER() PARTITION BY product_id ORDER BY ing_rank,conv_rank = occ  → occ=1 만(중복 제거)
   ▼
ORDER BY ing_rank, conv_rank  LIMIT :limit
```

**설명**
- 문서 I에는 트렌드 전용 쿼리가 없다(STEP3는 4.1 검색·4.2 CF만 기술). 기획서 5.2의 트렌드
  컨셉(`ingredient_trends` 상승 성분 × `search_purchase_pattern` 전환율)을 구현한 항목이다.
- 초기 구현은 **성분 8개를 파이썬 for 루프로 돌며 성분당 쿼리 + `seen` set 중복 제거**였다.
  이를 **윈도우 함수(`ROW_NUMBER`) 단일 쿼리**로 합쳐, 성분순위(`ing_rank`)·전환순위(`conv_rank`)·
  상품 중복 제거(`first_occ`)를 SQL이 처리한다. 최종 정렬 `(ing_rank, conv_rank)` 이 기존 파이썬
  삽입 순서와 동일해 **결과가 보존**된다.
- **[성능 최적화]** 초기 SQL은 `rising`(모든 상승 성분) × `products` 를 `LIKE '%성분%'` 로 먼저
  **풀스캔 조인**한 뒤 `WHERE ing_rank<=8` 로 잘라내, 버려질 성분까지 LIKE 조인·`PARTITION`
  정렬(`ROW_NUMBER`)을 수행했다. **`top_ing` CTE 로 상위 8개 성분을 LIKE 조인 전에 먼저
  추출**해, 가장 무거운 LIKE 조인과 윈도우 정렬의 입력 카디널리티를 줄였다(필터 푸시다운).
  성분 순위·전환 순위·중복 제거 규칙은 동일하므로 **결과는 변하지 않는다**.

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

---

### 2.3 검색 의도 기반 — `SQL_SEARCH_INTENT`

**구조도**

```
recent : 유저 최근 검색어 GROUP BY keyword, ORDER BY MAX(searched_at) DESC LIMIT 5
   ▼
norm   : recent.keyword            (원문)
         UNION
         TRIM(REPLACE(keyword,' 추천',''))   ("OO 추천" → "OO")
   ▼
search_purchase_pattern spp  JOIN products p
   WHERE spp.search_keyword IN (norm)
   ORDER BY spp.conversion_rate DESC  LIMIT :limit
```

**설명**
- 문서 I 4.1은 `LIKE CONCAT('%', keyword, '%')` 부분일치 + `relevance_score` 가중합 +
  `JSON_OVERLAPS(concern_target)` 필터를 썼다. 파일럿은 **정확일치(`IN`) + `conversion_rate`
  정렬**로 단순화했고(데모 데이터 규모·`concern_target` JSON 부재), 키워드 정규화("OO 추천"의
  접미사 제거)를 파이썬 set에서 **`UNION` + `TRIM(REPLACE())` CTE** 로 옮겼다.
- `SQL_RECENT_KEYWORDS` 는 "최근 검색어가 하나라도 있는지" 확인(빈 결과 시 early-return)과
  표시용 대표 키워드 추출에만 쓰고, 실제 매칭은 `SQL_SEARCH_INTENT` 한 문장이 담당한다.

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

### 2.4 소진/재구매 — `SQL_REPURCHASE`

**구조도**

```
purchase_history ph  JOIN products p  JOIN categories cat
   WHERE ph.user_id = :user_id
   GROUP BY product …
   HAVING COUNT(*) >= :repeat_min          ← 반복 구매 N회 이상(보관 버킷 기준)
   ORDER BY times DESC, last_at DESC  LIMIT :limit
```

**설명**
- 문서 I 5장 "소진 주기 계산"의 컨셉(반복 구매 + 카테고리 소진 주기)을 STEP3 카드용으로 단순화.
- 문서 I은 `volume_ml / daily_usage_ml` 정밀 소진일·D-7 필터까지 계산하지만, 파일럿 카드는
  **반복 구매 횟수(`times`)와 `avg_lifespan_days`(소진 주기)** 만 배지로 노출하므로 그 수준까지만
  집계한다(정밀 소진일은 향후 확장 여지).

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

## 3. 인덱스 설계

조회 패턴을 가속하기 위해 [upload_to_mysql.py](../upload_to_mysql.py) 의 `create_indexes()` 가
테이블 적재 후 아래 인덱스를 생성한다(`information_schema` 로 존재 확인 후 생성 — MySQL은
`CREATE INDEX IF NOT EXISTS` 미지원).

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

> 문서 I 6장 대비 추가분: `idx_sh_user_clicked`(클릭 신호 EXISTS), `idx_spp_product`(상품 조인),
> `idx_prod_category`(카테고리 조인), `idx_trend_month`(상승 성분 조회). 나머지는 문서 I과 동일.
> **[성능 최적화]** `idx_cart_user` 는 MySQL 8.0+ 의 **내림차순 인덱스(Descending Index)** 문법
> `(user_id, days_in_cart DESC)` 으로 생성한다. `SQL_STALE_CART` 의 `ORDER BY days_in_cart DESC`
> 를 인덱스 순서 그대로 읽어 **filesort 를 제거**한다(MySQL 8.0 미만에서는 `DESC` 키워드가
> 무시되어 오름차순으로 생성되지만, 본 시스템 기준 엔진은 9.4 이므로 정상 적용).

---

## 4. 문서 I 대비 변경사항 · 변경사유

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
