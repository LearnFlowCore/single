"""A small allowlist-only HTTP server for temporary public media hosting."""

from __future__ import annotations

import ipaddress
import secrets
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit

from .media import MediaInfo, validate_media


class PublicMediaServer:
    """Serve selected local files at unguessable routes.

    ``public_base_url`` must point to this server through externally configured
    DNS, port forwarding, or a tunnel. This class does not expose local ports.
    """

    def __init__(
        self,
        files: Iterable[str | Path],
        *,
        public_base_url: str,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.public_base_url = _validate_public_base_url(public_base_url)
        media = validate_media(files)
        if not media:
            raise ValueError("At least one local media file is required")
        self._routes = _make_routes(media)
        handler = partial(_MediaHandler, routes=self._routes)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def urls(self) -> dict[Path, str]:
        return {
            path: f"{self.public_base_url}{quote(route, safe='/')}"
            for route, (path, _) in self._routes.items()
        }

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def url_for(self, file: str | Path) -> str:
        path = Path(file).expanduser().resolve()
        try:
            return self.urls[path]
        except KeyError as exc:
            raise ValueError(f"File was not selected for hosting: {file}") from exc

    def start(self) -> PublicMediaServer:
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="public-media-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()

    def __enter__(self) -> PublicMediaServer:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


def _make_routes(media: list[MediaInfo]) -> dict[str, tuple[Path, str]]:
    routes: dict[str, tuple[Path, str]] = {}
    for item in media:
        assert item.path is not None
        route = f"/media/{secrets.token_urlsafe(24)}{item.extension}"
        routes[route] = (item.path, item.mime_type)
    return routes


class _MediaHandler(BaseHTTPRequestHandler):
    server_version = "PublicMediaServer/1.0"

    def __init__(self, *args: object, routes: dict[str, tuple[Path, str]], **kwargs: object) -> None:
        self._routes = routes
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(include_body=False)

    def _serve(self, *, include_body: bool) -> None:
        route = urlsplit(self.path).path
        selected = self._routes.get(route)
        if selected is None:
            self.send_error(404, "Not found")
            return
        path, mime_type = selected
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=60")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if include_body:
            try:
                with path.open("rb") as source:
                    while chunk := source.read(64 * 1024):
                        self.wfile.write(chunk)
            except (OSError, BrokenPipeError):
                pass

    def log_message(self, format: str, *args: object) -> None:
        return


def _validate_public_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public_base_url must be an externally reachable HTTP(S) URL")
    hostname = parsed.hostname
    if not hostname or hostname.lower() == "localhost":
        raise ValueError("public_base_url must not use a local-only hostname")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("public_base_url must use a public IP address or hostname")
    return value.rstrip("/")
