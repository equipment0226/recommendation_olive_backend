# 올리브영 장바구니 클렌징 추천 시스템 — SQL 쿼리 문서

> 본 문서는 장바구니 상품 유형 판별 및 추천 로직 구현에 사용된 SQL 쿼리를 기술합니다.  
> 버킷 판별 우선순위(v0.3): **보관 > 고민 > 충동**  
> 모든 쿼리는 MySQL 8.0 기준으로 작성되었습니다.

---

## 목차

1. [테이블 정의 (DDL)](#1-테이블-정의)
2. [버킷 판별 쿼리](#2-버킷-판별-쿼리)
   - 2.1 보관 상품
   - 2.2 고민 상품
   - 2.3 충동 상품
   - 2.4 클렌징_시즌
   - 2.5 클렌징_니즈해결
   - 2.6 통합 판별 (CASE WHEN)
3. [클렌징 추천 쿼리](#3-클렌징-추천-쿼리)
4. [STEP 3 신규 추천 쿼리](#4-step-3-신규-추천-쿼리)
5. [소진 주기 계산](#5-소진-주기-계산)
6. [인덱스 설계](#6-인덱스-설계)

---

## 1. 테이블 정의

### 1.1 users
```sql
CREATE TABLE users (
  user_id       VARCHAR(36)  PRIMARY KEY,
  skin_type     VARCHAR(20)  NOT NULL COMMENT '지성/건성/복합/민감성',
  skin_concerns JSON                  COMMENT '["트러블","보습","미백"] 복수 선택',
  age_group     VARCHAR(10)
);
```

### 1.2 categories
```sql
CREATE TABLE categories (
  category_id         VARCHAR(36)  PRIMARY KEY,
  category_name       VARCHAR(50)  NOT NULL,
  parent_category     VARCHAR(50),
  avg_lifespan_days   INT          COMMENT '용량 없는 상품 fallback 소진일',
  is_consumable       BOOLEAN      DEFAULT TRUE,
  daily_usage_ml      FLOAT        COMMENT '카테고리별 하루 평균 사용량(ml)',
  lifespan_per_100ml  FLOAT        COMMENT '100ml당 평균 소진일 (참고용)'
);
```

### 1.3 products
```sql
CREATE TABLE products (
  product_id       VARCHAR(36)  PRIMARY KEY,
  category_id      VARCHAR(36)  NOT NULL,
  brand            VARCHAR(100),
  product_name     VARCHAR(200),
  key_ingredients  JSON         COMMENT '["세라마이드","히알루론산"]',
  concern_target   JSON         COMMENT '["트러블","보습"]',
  suitable_season  VARCHAR(20)  COMMENT 'spring/summer/fall/winter/all',
  texture          VARCHAR(30),
  volume_ml        INT          COMMENT '용량 수치 (NULL 허용)',
  volume_unit      VARCHAR(10)  COMMENT 'ml/g/매/개',
  price            INT,
  is_available     BOOLEAN      DEFAULT TRUE,

  FOREIGN KEY (category_id) REFERENCES categories(category_id)
);
```

### 1.4 cart_items
```sql
CREATE TABLE cart_items (
  cart_id       VARCHAR(36)  PRIMARY KEY,
  user_id       VARCHAR(36)  NOT NULL,
  product_id    VARCHAR(36)  NOT NULL,
  added_at      DATETIME     NOT NULL COMMENT '장바구니 담은 시각 — 판별 기준 시점',
  days_in_cart  INT          GENERATED ALWAYS AS (DATEDIFF(NOW(), added_at)) STORED,
  referrer      VARCHAR(50)  COMMENT 'direct/search/event_banner/sns_ad/recommendation',
  quantity      INT          DEFAULT 1,

  FOREIGN KEY (user_id)    REFERENCES users(user_id),
  FOREIGN KEY (product_id) REFERENCES products(product_id)
);
```

### 1.5 purchase_history
```sql
CREATE TABLE purchase_history (
  purchase_id  VARCHAR(36)  PRIMARY KEY,
  user_id      VARCHAR(36)  NOT NULL,
  product_id   VARCHAR(36)  NOT NULL,
  purchased_at DATETIME     NOT NULL,
  quantity     INT          DEFAULT 1,
  paid_price   INT,

  FOREIGN KEY (user_id)    REFERENCES users(user_id),
  FOREIGN KEY (product_id) REFERENCES products(product_id)
);
```

### 1.6 search_history
```sql
CREATE TABLE search_history (
  search_id       VARCHAR(36)   PRIMARY KEY,
  user_id         VARCHAR(36)   NOT NULL,
  search_keyword  VARCHAR(200)  NOT NULL,
  searched_at     DATETIME      NOT NULL,
  product_clicked VARCHAR(36)   COMMENT '검색 후 클릭한 상품 ID (NULL 허용)',

  FOREIGN KEY (user_id) REFERENCES users(user_id)
);
```

### 1.7 search_purchase_pattern
```sql
CREATE TABLE search_purchase_pattern (
  pattern_id      VARCHAR(36)   PRIMARY KEY,
  search_keyword  VARCHAR(200)  NOT NULL,
  product_id      VARCHAR(36)   NOT NULL,
  purchase_count  INT           DEFAULT 0,
  conversion_rate FLOAT         COMMENT '검색 → 구매 전환율 (0.0~1.0)',

  FOREIGN KEY (product_id) REFERENCES products(product_id)
);
```

### 1.8 ingredient_concerns
```sql
CREATE TABLE ingredient_concerns (
  ingredient     VARCHAR(100)  NOT NULL,
  concern        VARCHAR(100)  NOT NULL,
  efficacy_level VARCHAR(10)   COMMENT '상/중/하',
  mechanism      VARCHAR(500),

  PRIMARY KEY (ingredient, concern)
);
```

### 1.9 ingredient_trends
```sql
CREATE TABLE ingredient_trends (
  trend_id      VARCHAR(36)   PRIMARY KEY,
  ingredient    VARCHAR(100)  NOT NULL,
  month         VARCHAR(7)    NOT NULL COMMENT 'YYYY-MM',
  search_volume INT,
  trend_delta   FLOAT         COMMENT '전월 대비 증감률 (%)',

  UNIQUE KEY uq_ing_month (ingredient, month)
);
```

---

## 2. 버킷 판별 쿼리

> **판별 우선순위**: 보관 > 고민 > 충동  
> 동일 상품이 시간 흐름에 따라 버킷이 바뀔 수 있으므로 판별은 주기적으로 재계산한다.

### 2.1 보관 상품 판별
**조건**: 동일 상품 반복 구매 이력 2회 이상 + 모두 담기 전(added_at 이전) 날짜

```sql
-- 특정 유저의 장바구니 중 보관 상품 판별
SELECT
    ci.cart_id,
    ci.product_id,
    p.product_name,
    ci.added_at,
    ci.days_in_cart,
    COUNT(ph.purchase_id)   AS repeat_purchase_count,
    MAX(ph.purchased_at)    AS last_purchase_date,
    '보관'                  AS bucket
FROM cart_items ci
JOIN products p ON ci.product_id = p.product_id
JOIN purchase_history ph
    ON  ph.user_id     = ci.user_id
    AND ph.product_id  = ci.product_id
    AND ph.purchased_at < ci.added_at   -- ★ 반드시 담기 전 구매
WHERE ci.user_id = :user_id
GROUP BY ci.cart_id, ci.product_id, p.product_name, ci.added_at, ci.days_in_cart
HAVING COUNT(ph.purchase_id) >= 2       -- ★ 2회 이상
ORDER BY ci.days_in_cart DESC;
```

### 2.2 고민 상품 판별
**조건**: 보관 조건 미충족 + 담은 후 동일 카테고리 재검색 1회 이상

```sql
WITH 보관_상품 AS (
    -- 보관 조건 충족 상품 제외
    SELECT ci.cart_id
    FROM cart_items ci
    JOIN purchase_history ph
        ON  ph.user_id    = ci.user_id
        AND ph.product_id = ci.product_id
        AND ph.purchased_at < ci.added_at
    WHERE ci.user_id = :user_id
    GROUP BY ci.cart_id
    HAVING COUNT(ph.purchase_id) >= 2
)
SELECT
    ci.cart_id,
    ci.product_id,
    p.product_name,
    ci.added_at,
    ci.days_in_cart,
    COUNT(sh.search_id) AS recheck_count,
    '고민'              AS bucket
FROM cart_items ci
JOIN products p   ON ci.product_id  = p.product_id
JOIN categories c ON p.category_id  = c.category_id
JOIN search_history sh
    ON  sh.user_id        = ci.user_id
    AND sh.searched_at   >= ci.added_at    -- ★ 담은 후 재검색
    AND sh.search_keyword LIKE CONCAT('%', c.category_name, '%')
WHERE ci.user_id = :user_id
  AND ci.cart_id NOT IN (SELECT cart_id FROM 보관_상품)
GROUP BY ci.cart_id, ci.product_id, p.product_name, ci.added_at, ci.days_in_cart
HAVING COUNT(sh.search_id) >= 1
ORDER BY recheck_count DESC;
```

### 2.3 충동 상품 판별
**조건**: 보관·고민 조건 모두 불충족 (기본값)  
referrer = event_banner / sns_ad 이면 충동 신호 강화

```sql
WITH 보관_상품 AS (
    SELECT ci.cart_id
    FROM cart_items ci
    JOIN purchase_history ph
        ON ph.user_id = ci.user_id AND ph.product_id = ci.product_id
        AND ph.purchased_at < ci.added_at
    WHERE ci.user_id = :user_id
    GROUP BY ci.cart_id HAVING COUNT(*) >= 2
),
고민_상품 AS (
    SELECT ci.cart_id
    FROM cart_items ci
    JOIN products p ON ci.product_id = p.product_id
    JOIN categories c ON p.category_id = c.category_id
    JOIN search_history sh
        ON sh.user_id = ci.user_id AND sh.searched_at >= ci.added_at
        AND sh.search_keyword LIKE CONCAT('%', c.category_name, '%')
    WHERE ci.user_id = :user_id
      AND ci.cart_id NOT IN (SELECT cart_id FROM 보관_상품)
    GROUP BY ci.cart_id HAVING COUNT(*) >= 1
)
SELECT
    ci.cart_id,
    ci.product_id,
    p.product_name,
    ci.added_at,
    ci.days_in_cart,
    ci.referrer,
    CASE
        WHEN ci.referrer IN ('event_banner','sns_ad') THEN '충동(배너유입확정)'
        ELSE '충동'
    END AS bucket
FROM cart_items ci
JOIN products p ON ci.product_id = p.product_id
WHERE ci.user_id = :user_id
  AND ci.cart_id NOT IN (SELECT cart_id FROM 보관_상품)
  AND ci.cart_id NOT IN (SELECT cart_id FROM 고민_상품)
ORDER BY ci.days_in_cart DESC;
```

### 2.4 클렌징_시즌 판별
**조건**: (보관·고민·충동 중 하나) + suitable_season이 현재 계절과 불일치

```sql
-- 현재 계절: spring (월 기준 3~5월)
SELECT
    ci.cart_id,
    ci.product_id,
    p.product_name,
    p.suitable_season,
    ci.added_at,
    CASE MONTH(NOW())
        WHEN 3 THEN 'spring' WHEN 4 THEN 'spring' WHEN 5 THEN 'spring'
        WHEN 6 THEN 'summer' WHEN 7 THEN 'summer' WHEN 8 THEN 'summer'
        WHEN 9 THEN 'fall'   WHEN 10 THEN 'fall'  WHEN 11 THEN 'fall'
        ELSE 'winter'
    END                         AS current_season,
    '클렌징_시즌'               AS cleansing_reason
FROM cart_items ci
JOIN products p ON ci.product_id = p.product_id
WHERE ci.user_id = :user_id
  AND p.suitable_season NOT IN ('all', 
      CASE MONTH(NOW())
          WHEN 3 THEN 'spring' WHEN 4 THEN 'spring' WHEN 5 THEN 'spring'
          WHEN 6 THEN 'summer' WHEN 7 THEN 'summer' WHEN 8 THEN 'summer'
          WHEN 9 THEN 'fall'   WHEN 10 THEN 'fall'  WHEN 11 THEN 'fall'
          ELSE 'winter'
      END)
ORDER BY ci.days_in_cart DESC;
```

### 2.5 클렌징_니즈해결 판별
**조건**: 담은 후 동일 카테고리 다른 상품 구매 완료

```sql
SELECT
    ci.cart_id,
    ci.product_id,
    p.product_name,
    ci.added_at,
    ph_after.product_id   AS purchased_product_id,
    ph_after.purchased_at AS purchased_date,
    '클렌징_니즈해결'      AS cleansing_reason
FROM cart_items ci
JOIN products p ON ci.product_id = p.product_id
JOIN purchase_history ph_after
    ON  ph_after.user_id     = ci.user_id
    AND ph_after.purchased_at > ci.added_at   -- ★ 담은 후 구매
    AND ph_after.product_id  != ci.product_id  -- 다른 상품
JOIN products p_after ON ph_after.product_id = p_after.product_id
    AND p_after.category_id = p.category_id   -- ★ 같은 카테고리
WHERE ci.user_id = :user_id
ORDER BY ci.days_in_cart DESC;
```

### 2.6 통합 판별 쿼리 (CASE WHEN)
**모든 버킷을 단일 쿼리로 판별** — 우선순위: 보관 > 고민 > 충동, 클렌징 조건은 오버레이

```sql
WITH
-- 보관 조건: 담기 전 동일 상품 구매 2회+
보관_cte AS (
    SELECT ci.cart_id, COUNT(*) AS repeat_cnt
    FROM cart_items ci
    JOIN purchase_history ph
        ON  ph.user_id    = ci.user_id
        AND ph.product_id = ci.product_id
        AND ph.purchased_at < ci.added_at
    WHERE ci.user_id = :user_id
    GROUP BY ci.cart_id
    HAVING COUNT(*) >= 2
),
-- 고민 조건: 담은 후 동일 카테고리 재검색 1회+
고민_cte AS (
    SELECT ci.cart_id, COUNT(sh.search_id) AS search_cnt
    FROM cart_items ci
    JOIN products p   ON ci.product_id = p.product_id
    JOIN categories c ON p.category_id  = c.category_id
    JOIN search_history sh
        ON  sh.user_id        = ci.user_id
        AND sh.searched_at   >= ci.added_at
        AND sh.search_keyword LIKE CONCAT('%', c.category_name, '%')
    WHERE ci.user_id = :user_id
    GROUP BY ci.cart_id
    HAVING COUNT(sh.search_id) >= 1
),
-- 니즈해결 조건: 담은 후 같은 카테고리 다른 상품 구매
니즈_cte AS (
    SELECT ci.cart_id
    FROM cart_items ci
    JOIN products p ON ci.product_id = p.product_id
    WHERE ci.user_id = :user_id
      AND EXISTS (
          SELECT 1 FROM purchase_history ph
          JOIN products p2 ON ph.product_id = p2.product_id
          WHERE ph.user_id      = ci.user_id
            AND p2.category_id  = p.category_id
            AND ph.product_id  != ci.product_id
            AND ph.purchased_at > ci.added_at
      )
),
-- 현재 계절 계산
season_now AS (
    SELECT CASE MONTH(NOW())
        WHEN 3 THEN 'spring' WHEN 4 THEN 'spring' WHEN 5 THEN 'spring'
        WHEN 6 THEN 'summer' WHEN 7 THEN 'summer' WHEN 8 THEN 'summer'
        WHEN 9 THEN 'fall'   WHEN 10 THEN 'fall'  WHEN 11 THEN 'fall'
        ELSE 'winter'
    END AS current_season
)
SELECT
    ci.cart_id,
    ci.user_id,
    ci.product_id,
    p.product_name,
    c.category_name,
    p.suitable_season,
    ci.added_at,
    ci.days_in_cart,
    ci.referrer,
    -- ★ 버킷 판별 (우선순위: 보관 > 고민 > 충동)
    CASE
        WHEN 니즈_cte.cart_id IS NOT NULL
            THEN '클렌징_니즈해결'
        WHEN p.suitable_season NOT IN ('all', sn.current_season)
            THEN '클렌징_시즌'
        WHEN 보관_cte.cart_id IS NOT NULL
            THEN '보관'
        WHEN 고민_cte.cart_id IS NOT NULL
            THEN '고민'
        ELSE '충동'
    END AS bucket
FROM cart_items ci
JOIN products    p  ON ci.product_id  = p.product_id
JOIN categories  c  ON p.category_id  = c.category_id
CROSS JOIN season_now sn
LEFT JOIN 보관_cte  ON 보관_cte.cart_id  = ci.cart_id
LEFT JOIN 고민_cte  ON 고민_cte.cart_id  = ci.cart_id
LEFT JOIN 니즈_cte  ON 니즈_cte.cart_id  = ci.cart_id
WHERE ci.user_id = :user_id
ORDER BY
    FIELD(bucket, '클렌징_니즈해결','클렌징_시즌','보관','고민','충동'),
    ci.days_in_cart DESC;
```

---

## 3. 클렌징 추천 쿼리

### 3.1 클렌징 대상 상품 목록 + 이유 메시지

```sql
WITH bucket_result AS (
    -- 위 2.6 통합 판별 쿼리 결과
    SELECT ci.cart_id, ci.product_id, bucket
    FROM ... -- 2.6 쿼리 참조
    WHERE ci.user_id = :user_id
)
SELECT
    br.cart_id,
    br.product_id,
    p.product_name,
    p.price,
    br.bucket,
    -- 유저에게 보여줄 클렌징 이유 메시지
    CASE br.bucket
        WHEN '클렌징_충동'     THEN CONCAT(
            CASE ci.referrer
                WHEN 'event_banner' THEN '기획전 배너 클릭으로 담으셨는데'
                WHEN 'sns_ad'       THEN 'SNS 광고를 통해 담으셨는데'
                ELSE '검색 없이 담으셨는데'
            END,
            ' 이후 관심 신호가 없어요.')
        WHEN '클렌징_시즌'     THEN CONCAT(p.suitable_season, '용 제품인데 지금은 봄이에요. 지금 사면 가을까지 쓰기 어려울 수 있어요.')
        WHEN '클렌징_니즈해결' THEN '담으신 후 비슷한 상품을 이미 구매하셨어요. 니즈가 이미 해결됐을 수 있어요.'
        ELSE '기타 클렌징 이유'
    END AS cleansing_message
FROM bucket_result br
JOIN cart_items ci ON br.cart_id = ci.cart_id
JOIN products    p  ON br.product_id = p.product_id
WHERE br.bucket IN ('충동','클렌징_충동','클렌징_시즌','클렌징_니즈해결')
ORDER BY ci.days_in_cart DESC;
```

---

## 4. STEP 3 신규 추천 쿼리

### 4.1 검색 이력 기반 추천 (Content-based)

```sql
-- 유저 최근 30일 검색 키워드 추출
WITH recent_keywords AS (
    SELECT search_keyword, COUNT(*) AS search_freq
    FROM search_history
    WHERE user_id    = :user_id
      AND searched_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
    GROUP BY search_keyword
    ORDER BY search_freq DESC
    LIMIT 5
),
-- 키워드별 전환율 높은 상품 후보
keyword_candidates AS (
    SELECT
        spp.product_id,
        SUM(rk.search_freq * spp.conversion_rate) AS relevance_score,
        GROUP_CONCAT(DISTINCT rk.search_keyword)  AS matched_keywords
    FROM search_purchase_pattern spp
    JOIN recent_keywords rk
        ON spp.search_keyword LIKE CONCAT('%', rk.search_keyword, '%')
    GROUP BY spp.product_id
)
SELECT
    kc.product_id,
    p.product_name,
    p.price,
    p.suitable_season,
    kc.relevance_score,
    kc.matched_keywords
FROM keyword_candidates kc
JOIN products p ON kc.product_id = p.product_id
JOIN users    u ON u.user_id     = :user_id
WHERE p.is_available = TRUE
  AND (p.suitable_season = :current_season OR p.suitable_season = 'all')
  AND JSON_OVERLAPS(p.concern_target, u.skin_concerns)
  AND kc.product_id NOT IN (
      SELECT product_id FROM cart_items
      WHERE user_id = :user_id
  )
  AND kc.product_id NOT IN (
      SELECT product_id FROM purchase_history
      WHERE user_id = :user_id
        AND purchased_at >= DATE_SUB(NOW(), INTERVAL 3 MONTH)
  )
ORDER BY kc.relevance_score DESC
LIMIT 10;
```

### 4.2 유사 유저 기반 추천 (Collaborative Filtering)

```sql
WITH similar_users AS (
    -- 피부 타입 + 피부 고민 기반 유사 유저 클러스터
    SELECT u.user_id
    FROM users u
    WHERE u.skin_type = (SELECT skin_type FROM users WHERE user_id = :user_id)
      AND u.user_id  != :user_id
      AND JSON_OVERLAPS(
          u.skin_concerns,
          (SELECT skin_concerns FROM users WHERE user_id = :user_id)
      )
),
cf_candidates AS (
    SELECT
        ph.product_id,
        COUNT(DISTINCT ph.user_id) AS purchase_count
    FROM purchase_history ph
    WHERE ph.user_id IN (SELECT user_id FROM similar_users)
      AND ph.purchased_at >= DATE_SUB(NOW(), INTERVAL 6 MONTH)
    GROUP BY ph.product_id
    HAVING COUNT(DISTINCT ph.user_id) >= 3
)
SELECT
    cf.product_id,
    p.product_name,
    p.price,
    cf.purchase_count,
    ROUND(cf.purchase_count / (SELECT COUNT(*) FROM similar_users) * 100, 1) AS purchase_rate_pct
FROM cf_candidates cf
JOIN products p ON cf.product_id = p.product_id
WHERE p.is_available = TRUE
  AND (p.suitable_season = :current_season OR p.suitable_season = 'all')
  AND cf.product_id NOT IN (
      SELECT product_id FROM cart_items WHERE user_id = :user_id
  )
  AND cf.product_id NOT IN (
      SELECT product_id FROM purchase_history
      WHERE user_id = :user_id
        AND purchased_at >= DATE_SUB(NOW(), INTERVAL 3 MONTH)
  )
ORDER BY purchase_rate_pct DESC
LIMIT 10;
```

---

## 5. 소진 주기 계산

```sql
-- 보관 상품의 소진 예상일 및 리마인드 타이밍 계산
SELECT
    ph_summary.user_id,
    ph_summary.product_id,
    p.product_name,
    p.volume_ml,
    c.daily_usage_ml,
    c.avg_lifespan_days,
    ph_summary.last_purchase_date,
    -- 정밀 소진 주기: volume_ml ÷ daily_usage_ml
    -- fallback: avg_lifespan_days (volume_ml 없는 경우)
    CASE
        WHEN p.volume_ml IS NOT NULL AND c.daily_usage_ml > 0
        THEN ROUND(p.volume_ml / c.daily_usage_ml)
        ELSE c.avg_lifespan_days
    END AS estimated_lifespan_days,
    -- 소진 예상일
    DATE_ADD(
        ph_summary.last_purchase_date,
        INTERVAL CASE
            WHEN p.volume_ml IS NOT NULL AND c.daily_usage_ml > 0
            THEN ROUND(p.volume_ml / c.daily_usage_ml)
            ELSE c.avg_lifespan_days
        END DAY
    ) AS expected_depletion_date,
    -- 소진까지 남은 일수
    DATEDIFF(
        DATE_ADD(
            ph_summary.last_purchase_date,
            INTERVAL CASE
                WHEN p.volume_ml IS NOT NULL AND c.daily_usage_ml > 0
                THEN ROUND(p.volume_ml / c.daily_usage_ml)
                ELSE c.avg_lifespan_days
            END DAY
        ),
        NOW()
    ) AS days_until_depletion
FROM (
    SELECT user_id, product_id, MAX(purchased_at) AS last_purchase_date
    FROM purchase_history
    WHERE user_id = :user_id
    GROUP BY user_id, product_id
) ph_summary
JOIN products    p ON ph_summary.product_id = p.product_id
JOIN categories  c ON p.category_id         = c.category_id
WHERE c.is_consumable = TRUE
  -- D-7 이내 소진 예정 상품만
  AND DATEDIFF(
        DATE_ADD(ph_summary.last_purchase_date,
            INTERVAL CASE
                WHEN p.volume_ml IS NOT NULL AND c.daily_usage_ml > 0
                THEN ROUND(p.volume_ml / c.daily_usage_ml)
                ELSE c.avg_lifespan_days
            END DAY),
        NOW()
      ) BETWEEN 0 AND 7
ORDER BY days_until_depletion ASC;
```

---

## 6. 인덱스 설계

```sql
-- cart_items: 유저별 방치 상품 조회 빈번
CREATE INDEX idx_cart_user ON cart_items(user_id, days_in_cart DESC);
CREATE INDEX idx_cart_user_prod ON cart_items(user_id, product_id);

-- purchase_history: 유저별 + 상품별 + 날짜 범위 조회
CREATE INDEX idx_pu_user_prod_date ON purchase_history(user_id, product_id, purchased_at);
CREATE INDEX idx_pu_user_date ON purchase_history(user_id, purchased_at);

-- search_history: 유저별 + 날짜 + 키워드 조회
CREATE INDEX idx_sh_user_date ON search_history(user_id, searched_at);
CREATE INDEX idx_sh_keyword   ON search_history(search_keyword);

-- search_purchase_pattern: 키워드별 전환율 정렬
CREATE INDEX idx_spp_kw_conv ON search_purchase_pattern(search_keyword, conversion_rate DESC);

-- ingredient_trends: 성분별 월 조회
CREATE INDEX idx_trend_ing_month ON ingredient_trends(ingredient, month);
```

---

## 부록 — 판별 우선순위 흐름도

```
장바구니 상품 1개 진입
        │
        ▼
[담기 전 동일 상품 구매 ≥ 2회?]
   Yes → 📌 보관
   No  ↓
[담은 후 동일 카테고리 재검색 ≥ 1회?]
   Yes → 🤔 고민
   No  ↓
[충동 — 기본값]
        │
        ├─ suitable_season ≠ 현재 계절? → 🌸 클렌징_시즌
        └─ 담은 후 같은 카테고리 타상품 구매? → ✅ 클렌징_니즈해결
```
