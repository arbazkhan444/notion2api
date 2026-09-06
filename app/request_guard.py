"""Process-local admission control, including the entire streaming lifetime."""
from __future__ import annotations

import os
from collections import defaultdict, deque
from time import monotonic
from starlette.responses import JSONResponse


class RequestGuard:
    def __init__(self, app, max_concurrent=None, max_body=None, per_minute=None):
        self.app = app
        self.max_concurrent = max_concurrent or int(os.getenv('MAX_CONCURRENT_REQUESTS', '4'))
        self.max_body = max_body or int(os.getenv('MAX_REQUEST_BYTES', '1048576'))
        self.per_minute = per_minute or int(os.getenv('REQUESTS_PER_MINUTE', '20'))
        if min(self.max_concurrent, self.max_body, self.per_minute) < 1:
            raise ValueError('Request limits must be positive.')
        self.active = 0
        self.requests = defaultdict(deque)
        self.last_prune = 0.0

    async def _error(self, scope, receive, send, status, message):
        response = JSONResponse({'error': {'message': message, 'type': 'request_limit'}},
                                status_code=status, headers={'Retry-After': '60'} if status == 429 else {})
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path', '').rstrip('/') != '/v1/chat/completions' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        now = monotonic()
        if now - self.last_prune > 60:
            for key in list(self.requests):
                queue = self.requests[key]
                while queue and queue[0] <= now - 60:
                    queue.popleft()
                if not queue:
                    del self.requests[key]
            self.last_prune = now
        # Do not trust X-Forwarded-For here. Configure the ASGI server's trusted proxies.
        key = (scope.get('client') or ('unknown', 0))[0]
        queue = self.requests[key]
        while queue and queue[0] <= now - 60:
            queue.popleft()
        if len(queue) >= self.per_minute or self.active >= self.max_concurrent:
            return await self._error(scope, receive, send, 429, 'Request limit reached; retry later.')
        queue.append(now)
        self.active += 1
        try:
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                if message['type'] != 'http.request':
                    continue
                body.extend(message.get('body', b''))
                if len(body) > self.max_body:
                    return await self._error(scope, receive, send, 413, 'Request body is too large.')
                if not message.get('more_body', False):
                    break
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            await self.app(scope, replay, send)
        finally:
            self.active -= 1
