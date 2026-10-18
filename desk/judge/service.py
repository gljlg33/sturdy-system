"""Judge service: the only process that holds the Jev key.

Bots authenticate with the desk secret (X-Desk-Secret). The judge forwards one bot's
full battery to Jev in one call and returns the answers raw, with the exact model
version string. It makes no trading decision and knows nothing about positions.

Run:  JEV_API_KEY=... DESK_SECRET=... JEV_MODEL=<exact-version> python -m desk.judge.service
"""
from __future__ import annotations

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk.config import require_pinned_model
from desk.judge.backend import BackendError, JevBackend, MalformedQuestion, RateLimited
from desk.questions.base import OTHER

MAX_BODY = 64 * 1024


def validate_battery(questions) -> str | None:
    if not isinstance(questions, list) or not questions:
        return "questions must be a non-empty list"
    for q in questions:
        if not isinstance(q, dict) or not all(isinstance(q.get(k), (str, list)) for k in ("id", "text", "options")):
            return "each question needs id, text, options"
        if OTHER not in q["options"]:
            return f"question {q['id']!r} has no {OTHER!r} option"
    return None


def make_handler(backend: JevBackend, desk_secret: str, pinned_model: str):
    secret = desk_secret.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "desk-judge"

        def log_message(self, fmt, *args):  # default logger prints headers on errors; keep it quiet
            pass

        def _send(self, code: int, body: dict, headers: dict | None = None):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/v1/judge":
                return self._send(404, {"error": "not_found"})
            if not hmac.compare_digest(self.headers.get("X-Desk-Secret", "").encode(), secret):
                return self._send(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"error": "bad_size"})
            try:
                req = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"error": "bad_json"})

            if (err := validate_battery(req.get("questions"))) is not None:
                return self._send(422, {"error": "malformed_question", "detail": err})

            t0 = time.monotonic()
            try:
                res = backend.ask_battery(context=str(req.get("context", "")), questions=req["questions"])
            except RateLimited as e:
                hdr = {"Retry-After": str(int(e.retry_after_s))} if e.retry_after_s else None
                return self._send(429, {"error": "rate_limited"}, hdr)
            except MalformedQuestion as e:
                return self._send(422, {"error": "malformed_question", "detail": str(e)})
            except (BackendError, NotImplementedError) as e:
                return self._send(502, {"error": "backend_error", "detail": type(e).__name__})

            # Raw passthrough. The pinned model is reported alongside so the desk can compare,
            # but the judge does not filter or alter answers.
            return self._send(200, {
                "model_version": res.model_version,
                "pinned_model": pinned_model,
                "answers": res.answers,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            })

    return Handler


def serve(backend: JevBackend, desk_secret: str, pinned_model: str, host: str = "127.0.0.1", port: int = 8787):
    httpd = ThreadingHTTPServer((host, port), make_handler(backend, desk_secret, pinned_model))
    return httpd


def main():
    from desk.config import desk_secret
    from desk.judge.backend import TypeSafeJevBackend

    model = require_pinned_model(os.environ.get("JEV_MODEL", ""))
    key = os.environ.get("JEV_API_KEY")
    if not key:
        raise SystemExit("JEV_API_KEY not set")
    backend = TypeSafeJevBackend(api_key=key, model=model)
    del key
    httpd = serve(backend, desk_secret(), model,
                  os.environ.get("JUDGE_HOST", "127.0.0.1"), int(os.environ.get("JUDGE_PORT", "8787")))
    print(f"judge listening on {httpd.server_address}, model={model}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
