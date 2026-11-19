#!/usr/bin/env python3
"""
Serve institution records and screenshots addressed by university name.

    python api.py

    GET  /api/health
    GET  /api/institutions?q=Anjaneya
    GET  /api/screenshots?name=Anjaneya%20University          primary PNG
    GET  /api/screenshots/meta?name=Anjaneya%20University     which lists, and the files
    POST /api/screenshots?name=Anjaneya%20University          capture every matching list

The university name is the only required input. The database decides UGC,
AICTE, or both. Add &state=Rajasthan when more than one institution uses that name.
"""
import os
import threading
from urllib.parse import urlencode

from flask import Flask, jsonify, request, send_file

import pipeline

app = Flask(__name__)
_capture_lock = threading.Lock()


def _shot_url(row):
    params = {"name": row["name"]}
    if row["state"]:
        params["state"] = row["state"]
    return "/api/screenshots?" + urlencode(params)


def _row_payload(row):
    sources = pipeline.applicable_sources(row)
    return {
        "name": row["name"],
        "state": row["state"],
        "city": row["city"],
        "primary_source": row["primary_source"],
        "sources": sources,
        "ugc_type": row["ugc_type"],
        "ugc_status": row["ugc_status"],
        "region": row["region"],
        "district": row["district"],
        "screenshot": _shot_url(row),
        "ready": all(pipeline.png_path(source, row["name"], row["state"]).exists() for source in sources),
    }


def _query_name():
    name = (request.args.get("name") or "").strip()
    state = (request.args.get("state") or "").strip() or None
    city = (request.args.get("city") or "").strip() or None
    if not name:
        return None, (jsonify({"error": "pass name, the university name"}), 400)
    try:
        row = pipeline.fetch_institution(name, state, city)
    except pipeline.AmbiguousName as exc:
        return None, (jsonify({"error": str(exc), "matches": exc.matches}), 409)
    except LookupError as exc:
        return None, (jsonify({"error": str(exc)}), 404)
    return row, None


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "screenshots": str(pipeline.SHOTS)})


@app.get("/api/institutions")
def search_institutions():
    query = (request.args.get("q") or request.args.get("name") or "").strip()
    if not query:
        return jsonify({"error": "pass ?q= or ?name= a university name"}), 400
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    like = f"%{query}%"
    connection = pipeline.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM institutions
                WHERE name LIKE %s OR name_key = %s
                ORDER BY in_ugc DESC, name, state
                LIMIT %s
                """,
                (like, pipeline.name_key(query), limit),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return jsonify({"count": len(rows), "results": [_row_payload(row) for row in rows]})


def _primary_png(row):
    """UGC file when the university is on UGC, otherwise the AICTE file."""
    for source in pipeline.applicable_sources(row):
        out = pipeline.png_path(source, row["name"], row["state"])
        if out.exists():
            return out
    source = pipeline.applicable_sources(row)[0]
    return pipeline.png_path(source, row["name"], row["state"])


@app.get("/api/screenshots/meta")
def screenshot_meta():
    row, error = _query_name()
    if error:
        return error
    try:
        details = pipeline.describe(row["name"], row["state"], row["city"])
    except FileNotFoundError as exc:
        return jsonify({
            "error": str(exc),
            "name": row["name"],
            "state": row["state"],
            "sources": pipeline.applicable_sources(row),
            "capture": "POST " + _shot_url(row),
        }), 404
    return jsonify(details)


@app.get("/api/screenshots")
def screenshot_file():
    row, error = _query_name()
    if error:
        return error
    out = _primary_png(row)
    if not out.exists():
        return jsonify({
            "error": "screenshot has not been captured",
            "name": row["name"],
            "state": row["state"],
            "sources": pipeline.applicable_sources(row),
            "capture": "POST " + _shot_url(row),
        }), 404
    return send_file(out, mimetype="image/png")


@app.post("/api/screenshots")
def screenshot_capture():
    """Capture every list this university belongs to."""
    row, error = _query_name()
    if error:
        return error
    try:
        with _capture_lock:
            meta = pipeline.capture(row["name"], row["state"], row["city"])
    except pipeline.AmbiguousName as exc:
        return jsonify({"error": str(exc), "matches": exc.matches}), 409
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    meta = dict(meta)
    meta["url"] = _shot_url(row)
    return jsonify(meta), 201


def main():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
