#!/usr/bin/env python3
"""
Serve institution records and screenshots addressed by university name.

    python api.py

    GET  /api/health
    GET  /api/institutions?q=Anjaneya
    GET  /api/screenshots?name=Anjaneya%20University          primary PNG
    GET  /api/screenshots/meta?name=Anjaneya%20University     which lists, and the files
    POST /api/screenshots?name=Anjaneya%20University          capture every matching list
    POST /api/batch?source=ugc|aicte|all[&state=..&limit=..&skip_existing=1&dry_run=1]
                                                              start a background batch (202)
    GET  /api/batch                                           status of the current or last batch
    DELETE /api/batch                                         ask the running batch to stop

The university name is the only required input. The database decides UGC,
AICTE, or both. Add &state=Rajasthan when more than one institution uses that name.
"""
import logging
import os
import threading
from urllib.parse import urlencode

from flask import Flask, jsonify, request, send_file

import pipeline

pipeline.setup_logging()
log = logging.getLogger("pipeline.api")
app = Flask(__name__)
_capture_lock = threading.Lock()


def _shot_url(row):
    params = {"name": row["name"]}
    if row["state"]:
        params["state"] = row["state"]
    if row["city"]:  # harmless when unique, needed when a state has several of the name
        params["city"] = row["city"]
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
        "ready": all(pipeline.png_path(source, row).exists() for source in sources),
    }


def _query_name():
    name = (request.args.get("name") or "").strip()
    state = (request.args.get("state") or "").strip() or None
    city = (request.args.get("city") or "").strip() or None
    if not name:
        log.warning("%s %s: rejected, name missing", request.method, request.path)
        return None, (jsonify({"error": "pass name, the university name"}), 400)
    log.info("%s %s name=%r state=%r city=%r", request.method, request.path, name, state, city)
    try:
        row = pipeline.fetch_institution(name, state, city)
    except pipeline.AmbiguousName as exc:
        log.warning("%s %s: 409, %s required (%d matches)", request.method, request.path,
                    exc.missing, len(exc.matches))
        return None, (jsonify({
            "error": str(exc),
            "required": exc.missing,
            "hint": f"Repeat the request with &{exc.missing}=<value>",
            "matches": exc.matches,
        }), 409)
    except LookupError as exc:
        log.warning("%s %s: 404, %s", request.method, request.path, exc)
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
        out = pipeline.png_path(source, row)
        if out.exists():
            return out
    source = pipeline.applicable_sources(row)[0]
    return pipeline.png_path(source, row)


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
        return jsonify({"error": str(exc), "required": exc.missing, "matches": exc.matches}), 409
    except LookupError as exc:
        log.warning("POST capture: 404, %s", exc)
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        log.exception("POST capture: 500 for %s", row["name"])
        return jsonify({"error": str(exc)}), 500
    log.info("POST capture: 201, saved %s", [item["file"] for item in meta["files"]])
    meta = dict(meta)
    meta["url"] = _shot_url(row)
    return jsonify(meta), 201


_batch = {"thread": None, "progress": {}, "stop": False, "result": None}


def _batch_running():
    thread = _batch["thread"]
    return thread is not None and thread.is_alive()


def _run_batch(options):
    try:
        with _capture_lock:
            _batch["result"] = pipeline.capture_all(
                progress=_batch["progress"], should_stop=lambda: _batch["stop"], **options)
    except Exception as exc:
        log.exception("batch crashed")
        _batch["progress"].update(finished=True, error=str(exc))


def _flag(name):
    return (request.args.get(name) or "").lower() in ("1", "true", "yes")


@app.post("/api/batch")
def batch_start():
    source = (request.args.get("source") or "all").lower()
    if source not in pipeline.BATCH_SOURCES:
        return jsonify({"error": f"source must be one of {list(pipeline.BATCH_SOURCES)}"}), 400
    try:
        limit = int(request.args["limit"]) if request.args.get("limit") else None
        timeout = int(request.args.get("timeout", 90))
    except ValueError:
        return jsonify({"error": "limit and timeout must be integers"}), 400
    if _batch_running():
        log.warning("POST /api/batch: 409, a batch is already running")
        return jsonify({"error": "a batch is already running", "progress": _batch["progress"]}), 409
    options = {
        "source": source,
        "state": (request.args.get("state") or "").strip() or None,
        "limit": limit,
        "skip_existing": _flag("skip_existing"),
        "timeout": timeout,
        "dry_run": _flag("dry_run"),
    }
    log.info("POST /api/batch: starting %s", options)
    _batch.update(progress={}, stop=False, result=None)
    _batch["thread"] = threading.Thread(target=_run_batch, args=(options,), daemon=True)
    _batch["thread"].start()
    return jsonify({"started": options, "status": "/api/batch"}), 202


@app.get("/api/batch")
def batch_status():
    result = _batch["result"]
    return jsonify({
        "running": _batch_running(),
        "progress": _batch["progress"],
        "failures": (result or {}).get("failures", []),
    })


@app.delete("/api/batch")
def batch_stop():
    if not _batch_running():
        return jsonify({"error": "no batch is running"}), 404
    log.warning("DELETE /api/batch: stop requested")
    _batch["stop"] = True
    return jsonify({"stopping": True}), 202


def main():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
