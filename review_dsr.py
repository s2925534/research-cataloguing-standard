#!/usr/bin/env python3
"""Browser-based one-at-a-time review tool for DSR catalogue records flagged
confidence_status='Requires Review' in instance/catalogue_dsr.db.

Standard library only, matching this project's other local review tools.
Each decision (approve / edit / skip) writes directly to
instance/catalogue_dsr.db as you go - there's no separate "save" step, so
closing the browser or the server at any point loses nothing: progress is
just however many records no longer have confidence_status='Requires
Review'. Re-running this script later resumes on whatever's left.

Usage:
    python3 review_dsr.py [--port 8899] [--no-browser]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from catalogues import dsr_catalogue as dsr

ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "dsr_review_ui"


def get_db(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{dsr.DSR_DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(dsr.DSR_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _record_to_dict(row: sqlite3.Row) -> dict:
    try:
        legacy_ids = json.loads(row["legacy_ids_json"] or "[]")
    except json.JSONDecodeError:
        legacy_ids = []
    return {
        "catalogue_id": row["catalogue_id"],
        "stable_id": row["stable_id"],
        "legacy_ids": legacy_ids,
        "class_code": row["class_code"],
        "subtype_code": row["subtype_code"],
        "dsr_artefact_type": row["dsr_artefact_type"],
        "version": row["version"],
        "file_name": row["file_name"],
        "source_path": row["source_path"],
        "classification_rule": row["classification_rule"],
        "classification_evidence": row["classification_evidence"],
        "notes": row["notes"],
    }


class ReviewHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", status)

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        if not path.is_file():
            self.send_error(404)
            return
        self._send_bytes(path.read_bytes(), f"{content_type}; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_static("index.html", "text/html")
        elif path == "/app.js":
            self._serve_static("app.js", "application/javascript")
        elif path == "/style.css":
            self._serve_static("style.css", "text/css")
        elif path == "/api/records":
            conn = get_db(readonly=True)
            total = conn.execute(
                "SELECT COUNT(*) c FROM dsr_catalogue WHERE confidence_status = ? AND status != 'excluded'",
                (dsr.REQUIRES_REVIEW,),
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM dsr_catalogue WHERE confidence_status = ? AND status != 'excluded' "
                "ORDER BY classification_rule, catalogue_id",
                (dsr.REQUIRES_REVIEW,),
            ).fetchall()
            conn.close()
            self._send_json({"total_remaining": total, "records": [_record_to_dict(r) for r in rows]})
        elif path == "/api/vocab":
            self._send_json({
                "main_classes": dsr.MAIN_CLASSES,
                "artefact_subtypes": dsr.ARTEFACT_SUBTYPES,
            })
        else:
            self.send_error(404)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        if path == "/api/decide":
            self._handle_decide(payload)
        else:
            self.send_error(404)

    def _handle_decide(self, payload: dict) -> None:
        catalogue_id = payload.get("catalogue_id")
        action = payload.get("action")
        if not catalogue_id or action not in ("approve", "edit", "skip"):
            self._send_json({"error": "catalogue_id and a valid action are required"}, 400)
            return

        if action == "skip":
            self._send_json({"status": "skipped"})
            return

        conn = get_db()
        row = conn.execute("SELECT * FROM dsr_catalogue WHERE catalogue_id = ?", (catalogue_id,)).fetchone()
        if row is None:
            conn.close()
            self._send_json({"error": f"no such catalogue_id: {catalogue_id}"}, 404)
            return

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        class_code, subtype_code = row["class_code"], row["subtype_code"]
        note_suffix = f"human-approved via review_dsr.py on {now}"

        if action == "edit":
            class_code = payload.get("class_code") or class_code
            subtype_code = payload.get("subtype_code") or subtype_code
            if class_code not in dsr.MAIN_CLASSES:
                conn.close()
                self._send_json({"error": f"unknown class_code: {class_code}"}, 400)
                return
            if class_code == "ART" and subtype_code not in dsr.ARTEFACT_SUBTYPES:
                conn.close()
                self._send_json({"error": f"unknown ART subtype_code: {subtype_code}"}, 400)
                return
            dsr_artefact_type = (
                dsr.ARTEFACT_SUBTYPE_TO_CLASSIFICATION.get(subtype_code, dsr.NOT_ASSIGNED)
                if class_code == "ART" else dsr.NOT_APPLICABLE
            )
            note_suffix = f"human-edited ({row['class_code']}/{row['subtype_code']} -> {class_code}/{subtype_code}) via review_dsr.py on {now}"
            conn.execute(
                "UPDATE dsr_catalogue SET class_code = ?, subtype_code = ?, dsr_artefact_type = ?, "
                "confidence_status = 'Confident', notes = COALESCE(notes || ' | ', '') || ?, updated_at = ? "
                "WHERE catalogue_id = ?",
                (class_code, subtype_code, dsr_artefact_type, note_suffix, now, catalogue_id),
            )
        else:  # approve
            conn.execute(
                "UPDATE dsr_catalogue SET confidence_status = 'Confident', "
                "notes = COALESCE(notes || ' | ', '') || ?, updated_at = ? WHERE catalogue_id = ?",
                (note_suffix, now, catalogue_id),
            )
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM dsr_catalogue WHERE confidence_status = ? AND status != 'excluded'",
            (dsr.REQUIRES_REVIEW,),
        ).fetchone()["c"]
        conn.close()
        self._send_json({"status": "ok", "remaining": remaining})

    def log_message(self, format, *args):  # noqa: A002
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not dsr.DSR_DB_PATH.exists():
        raise SystemExit(
            f"No DSR database at {dsr.DSR_DB_PATH}. Run a --dsr scan or "
            f"'python3 migrate_legacy_to_dsr.py crosswalk --apply' first."
        )

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReviewHandler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"DSR review tool running at {url} (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
