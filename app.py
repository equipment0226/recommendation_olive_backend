"""올리브영 장바구니 클렌징·추천 — Flask REST API.

개발: python app.py  →  http://127.0.0.1:5000 (API)
      frontend/에서 npm run dev  →  http://127.0.0.1:5173 (Vite 프록시)
배포: gunicorn app:app  (Railway PORT 환경변수 자동 적용)
"""
from __future__ import annotations

import os
import traceback

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

# 개발(Vite dev server) 및 프로덕션 프론트 오리진 허용
CORS(app, origins=[
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://web-production-90966.up.railway.app",
])


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "backend": config.DATA_BACKEND})


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
