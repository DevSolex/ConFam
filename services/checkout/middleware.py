"""
services/checkout/middleware.py — Rate limiting for public checkout endpoints.

The checkout service (port 8001) is publicly reachable — buyers open links
from WhatsApp. Without rate limiting, the pay/bank endpoint can be hammered
to scrape merchant subaccount codes or exhaust Paystack API quota.

Implementation: simple in-process sliding window per IP using a dict.
For production, replace with Redis-backed rate limiting (or a WAF rule)
that works across multiple instances. The in-process approach is correct
for a single-instance pilot.

Limits (conservative, adjustable via env vars):
  RATE_LIMIT_PAY_RPM      — POST /{link_id}/pay[/bank]  (default: 10/min per IP)
  RATE_LIMIT_CHECKOUT_RPM — GET /{link_id}, GET /{link_id}/status
                                                  (default: 60/min per IP)

The checkout GET limit has to cover polling: after paying, a buyer sits on the
page hitting /{link_id}/status every 3 seconds, so ~20 requests per minute per
buyer, all from the same IP behind Render's proxy. If buyers start seeing 429s
mid-payment, raise CHECKOUT_RPM before doing anything else.
"""

import os
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import structlog

log = structlog.get_logger()


def _rpm(env_key: str, default: int) -> int:
    try:
        return int(os.environ.get(env_key, str(default)))
    except (ValueError, TypeError):
        return default


PAY_RPM = _rpm("RATE_LIMIT_PAY_RPM", 10)
CHECKOUT_RPM = _rpm("RATE_LIMIT_CHECKOUT_RPM", 60)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter for public checkout endpoints.
    Keyed by client IP + path prefix.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        # {(ip, path_key): deque of timestamps within the current window}
        self._windows: dict = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"

        # Determine limit for this path.
        #
        # The pay path check is a suffix match, not the old "/pay/bank" substring
        # test, so it covers both the canonical POST /{link_id}/pay and the
        # legacy POST /{link_id}/pay/bank without also catching a link_id that
        # happens to contain those characters. It is last for a reason: the GET
        # branch below would otherwise swallow the poll endpoint, which is hit
        # every 3 seconds by every buyer who just paid.
        if request.method == "POST" and (path.endswith("/pay") or path.endswith("/pay/bank")):
            limit = PAY_RPM
            path_key = "pay"
        elif request.method == "GET" and path != "/health":
            limit = CHECKOUT_RPM
            path_key = "checkout"
        else:
            return await call_next(request)

        window_key = (client_ip, path_key)
        now = time.monotonic()
        window = self._windows[window_key]

        # Remove timestamps older than 60 seconds
        while window and window[0] < now - 60:
            window.popleft()

        if len(window) >= limit:
            log.warning(
                "rate_limit_exceeded",
                client_ip=client_ip,
                path=path,
                limit=limit,
                window="60s",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "too_many_requests",
                    "detail": "Rate limit exceeded. Please wait before retrying.",
                    "retry_after_seconds": 60,
                },
            )

        window.append(now)
        return await call_next(request)
