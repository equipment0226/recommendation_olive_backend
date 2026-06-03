"""올리브영 장바구니 클렌징·추천 — Flask REST API.

개발: python app.py  →  http://127.0.0.1:5000 (API)
      frontend/에서 npm run dev  →  http://127.0.0.1:5173 (Vite 프록시)
배포: gunicorn app:app  (Railway PORT 환경변수 자동 적용)
"""
from __future__ import annotations

import json
import os
import traceback
import urllib.parse
import urllib.request

from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

import config
from recommendation.bucket import get_cart_analysis, validate_all
from recommendation.db import get_db
from recommendation.recommender import (
    ALL_RECOMMENDERS,
    get_recommendation_options,
)

app = Flask(__name__)

# API 전용 서버 — 모든 오리진 허용 (Vercel 프론트 등 어디서든 호출 가능)
CORS(app, resources={r"/api/*": {"origins": "*"}})


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "backend": config.DATA_BACKEND,
        "version": "v2",
        "mysql_host": config.MYSQL["host"],
        "mysql_port": config.MYSQL["port"],
    })


@app.get("/api/users")
def api_users():
    """데모용 전체 유저 목록 (GNB 드롭다운)."""
    db = get_db()
    rows = db.query(
        """
        SELECT u.user_id, u.skin_type, u.skin_concerns, u.age_group,
               COUNT(c.cart_id) AS stale_cart
        FROM   users u
        LEFT JOIN cart_items c
               ON c.user_id = u.user_id AND c.days_in_cart >= :stale_days
        GROUP BY u.user_id, u.skin_type, u.skin_concerns, u.age_group
        ORDER BY stale_cart DESC, u.user_id
        """,
        {"stale_days": config.STALE_DAYS},
    )
    users = [{
        "user_id": r["user_id"],
        "skin_type": r["skin_type"],
        "skin_concerns": (r["skin_concerns"] or "").split("|"),
        "age_group": r["age_group"],
        "stale_cart": r["stale_cart"],
    } for r in rows]
    return jsonify(users)


@app.get("/api/analysis/<user_id>")
def api_analysis(user_id: str):
    """STEP1 현황요약 + STEP2 클렌징 분류 결과."""
    return jsonify(get_cart_analysis(user_id))


@app.get("/api/recommend/<user_id>")
def api_recommend(user_id: str):
    """STEP3 추천 옵션 카드 + 각 알고리즘 결과."""
    return jsonify(get_recommendation_options(user_id))


@app.get("/api/recommend/<user_id>/<algo>")
def api_recommend_one(user_id: str, algo: str):
    """단일 추천 알고리즘 결과."""
    fn = ALL_RECOMMENDERS.get(algo)
    if fn is None:
        return jsonify({"error": "unknown algorithm"}), 404
    return jsonify(fn(user_id))


@app.get("/api/validate")
def api_validate():
    """전체 분류 결과 vs cart_items.expected_bucket 검증(일치율)."""
    return jsonify(validate_all())


# ── Pexels 샘플 이미지 ────────────────────────────────────────────────
# category_id 별로 영어 쿼리로 Pexels 검색 → 이미지 URL 풀을 반환.
# 결과는 메모리에 캐싱하여 rate limit/지연을 줄이고 동일 이미지를 안정 제공.
_image_cache: dict[str, list[str]] = {}


@app.get("/api/images/<category_id>")
def api_images(category_id: str):
    """카테고리별 Pexels 샘플 이미지 URL 풀 (프론트가 중복 없이 배정)."""
    if category_id in _image_cache:
        return jsonify({"category_id": category_id, "images": _image_cache[category_id]})

    query = config.CATEGORY_IMAGE_QUERY.get(category_id, "cosmetic")
    q = urllib.parse.quote(f"{query} product")
    url = (
        f"https://api.pexels.com/v1/search?query={q}"
        "&per_page=30&orientation=square"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": config.PEXELS_API_KEY,
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        images = [
            p["src"]["medium"]
            for p in data.get("photos", [])
            if p.get("src", {}).get("medium")
        ]
        _image_cache[category_id] = images
        return jsonify({"category_id": category_id, "images": images})
    except Exception as e:  # 실패 시 빈 풀 → 프론트는 이모지 폴백
        return jsonify({"category_id": category_id, "images": [], "error": str(e)})


@app.get("/api/debug")
def api_debug():
    """배포 환경 진단용 엔드포인트."""
    import sys
    info: dict = {
        "python": sys.version,
        "backend": config.DATA_BACKEND,
        "mysql_host": config.MYSQL["host"],
        "mysql_port": config.MYSQL["port"],
        "mysql_db": config.MYSQL["database"],
    }
    try:
        import pymysql
        conn = pymysql.connect(
            host=config.MYSQL["host"],
            port=config.MYSQL["port"],
            user=config.MYSQL["user"],
            password=config.MYSQL["password"],
            database=config.MYSQL["database"],
            charset=config.MYSQL["charset"],
            connect_timeout=10,
        )
        conn.cursor().execute("SELECT 1")
        conn.close()
        info["mysql_connect"] = "ok"
    except Exception as e:
        info["mysql_connect"] = str(e)
    return jsonify(info)


@app.errorhandler(Exception)
def handle_exception(e):
    """500 에러 시 traceback을 JSON으로 반환 (디버깅용)."""
    # 404 등 HTTP 예외는 원래 상태코드 그대로 반환
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    return jsonify({
        "error": str(e),
        "traceback": traceback.format_exc(),
    }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() != "false"
    app.run(debug=debug, host="0.0.0.0", port=port)
