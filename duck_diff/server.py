"""Air-gapped HTTP server for the duck-diff Web Studio dashboard.

Serves the interactive dashboard on ``http://localhost:8090`` using only
the Python standard-library :mod:`http.server`.  No Flask, no uvicorn,
no network dependencies.

Endpoints
---------
* ``GET /``               — Web Studio dashboard (HTML).
* ``GET /pricing``        — Pricing comparison page.
* ``POST /api/diff``      — Accept a JSON diff request, run the engine,
  return the full :class:`DiffResult` payload.
* ``GET /api/status``     — Engine health / version check.
"""

from __future__ import annotations

import io
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from . import __version__
from .cli import execute_diff, parse_keys
from .engine import DiffResult
from .license import LicenseTier, get_license_info
from .reporter import to_json
from .webstudio import pricing_page, studio_page

__all__ = ["StudioServer", "serve_studio"]

_DEFAULT_PORT = 8090
_DEFAULT_HOST = "127.0.0.1"


class StudioHandler(BaseHTTPRequestHandler):
    """Request handler for the Web Studio dashboard."""

    server_version = f"duck-diff/{__version__}"

    # Silence per-request log lines in production; keep for debugging.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-duck-diff-version", __version__)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, "application/json; charset=utf-8", body)

    def _send_html(self, code: int, html: str) -> None:
        self._send(code, "text/html; charset=utf-8", html.encode("utf-8"))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(200, studio_page())
        elif path == "/pricing":
            self._send_html(200, pricing_page())
        elif path == "/api/status":
            lic = get_license_info()
            self._send_json(200, {
                "version": __version__,
                "tier": lic.tier,
                "license_valid": lic.is_valid,
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/diff":
            self._handle_diff()
        else:
            self._send_json(404, {"error": "not found"})

    # -- handlers ----------------------------------------------------------

    def _handle_diff(self) -> None:
        try:
            body = self._read_body()
            req = json.loads(body) if body else {}
        except Exception:  # noqa: BLE001
            self._send_json(400, {"error": "invalid JSON body"})
            return

        source = req.get("source")
        target = req.get("target")
        if not source or not target:
            self._send_json(400, {"error": "source and target are required"})
            return

        keys = parse_keys(req.get("key"))
        epsilon = float(req.get("epsilon", 0.0))
        ignore_case = bool(req.get("ignore_case", False))
        sample_limit = int(req.get("sample_limit", 20))
        fmt = req.get("format", "json")

        try:
            result: DiffResult = execute_diff(
                str(source),
                str(target),
                keys=keys,
                epsilon=epsilon,
                ignore_case=ignore_case,
                sample_limit=sample_limit,
            )
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            return

        if fmt == "json":
            self._send_json(200, result.to_dict())
        else:
            from .reporter import render

            text = render(result, fmt)
            self._send(200, "text/plain; charset=utf-8", text.encode("utf-8"))


def serve_studio(
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    *,
    quiet: bool = True,
) -> None:
    """Start the Web Studio HTTP server (blocking).

    Args:
        host: Bind address (default ``127.0.0.1``).
        port: Port number (default ``8090``).
        quiet: Suppress per-request logging.
    """
    server = HTTPServer((host, port), StudioHandler)
    if quiet:
        server.log_request = lambda *a, **kw: None  # type: ignore[assignment]
    print(f"duck-diff Web Studio running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


class StudioServer:
    """Object-oriented wrapper around :func:`serve_studio` for programmatic use."""

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self._httpd: Optional[HTTPServer] = None

    def start(self, *, blocking: bool = True) -> None:
        """Start the server.

        When *blocking* is ``False`` the server runs in a background thread.
        """
        self._httpd = HTTPServer((self.host, self.port), StudioHandler)
        if blocking:
            print(f"duck-diff Web Studio at http://{self.host}:{self.port}")
            try:
                self._httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                self._httpd.server_close()
        else:
            import threading

            t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            t.start()
            print(f"duck-diff Web Studio at http://{self.host}:{self.port}")

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def running(self) -> bool:
        return self._httpd is not None
