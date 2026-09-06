import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.account_pool import AccountPool
from app.api.chat import router as chat_router
from app.api.models import router as models_router
from app.config import ACCOUNTS, API_KEY, ALLOWED_ORIGINS, APP_MODE
from app.conversation import ConversationManager
from app.frontend_view import router as frontend_router, render_index
from app.logger import logger
from app.request_guard import RequestGuard


@asynccontextmanager
async def lifespan(app):
    if not API_KEY and os.getenv('ALLOW_UNAUTHENTICATED', '').strip().lower() != 'true':
        raise RuntimeError('Set API_KEY, or explicitly opt into trusted-local use with ALLOW_UNAUTHENTICATED=true.')
    if APP_MODE not in ('lite', 'standard', 'heavy'):
        raise RuntimeError('APP_MODE must be lite, standard, or heavy.')
    app.state.account_pool = await run_in_threadpool(AccountPool, ACCOUNTS)
    if APP_MODE == 'heavy':
        app.state.conversation_manager = await run_in_threadpool(ConversationManager)
    app.state.conversation_locks = {}
    app.state.start_time = time.time()
    await run_in_threadpool(render_index)
    logger.info('Service started', extra={'request_info': {'mode': APP_MODE, 'accounts': len(ACCOUNTS)}})
    yield
    logger.info('Service stopped')


app = FastAPI(title='Notion Opus API', version='1.1.0', lifespan=lifespan,
              description='Text-only OpenAI-compatible adapter. See docs/API_COMPATIBILITY.md for supported options.')
app.add_middleware(RequestGuard)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    details = [{'loc': list(error['loc']), 'msg': error['msg'], 'type': error['type']} for error in exc.errors()]
    logger.warning('Request validation failed', extra={'request_info': {'path': request.url.path}})
    return JSONResponse({'error': {'message': 'Request validation failed', 'type': 'invalid_request_error', 'details': details}}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_error(request, exc):
    logger.error('Unhandled application error', extra={'request_info': {'path': request.url.path, 'exception_type': type(exc).__name__}})
    return JSONResponse({'error': {'message': 'Internal server error', 'type': 'server_error'}}, status_code=500)


@app.middleware('http')
async def api_key_auth(request: Request, call_next):
    if API_KEY and request.url.path.startswith('/v1') and request.method != 'OPTIONS':
        scheme, _, supplied = request.headers.get('Authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not secrets.compare_digest(supplied.encode('utf-8'), API_KEY.encode('utf-8')):
            return JSONResponse({'error': {'message': 'Invalid API key.', 'type': 'invalid_request_error', 'code': 'invalid_api_key'}}, status_code=401)
    return await call_next(request)


app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_credentials='*' not in ALLOWED_ORIGINS,
                   allow_methods=['GET', 'POST', 'DELETE', 'OPTIONS'],
                   allow_headers=['Authorization', 'Content-Type', 'X-Client-Type'],
                   expose_headers=['X-Conversation-Id', 'X-Memory-Status', 'Retry-After'])
app.include_router(chat_router, prefix='/v1')
app.include_router(models_router, prefix='/v1')
app.include_router(frontend_router)


@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get('/health', tags=['system'])
async def health(request: Request):
    status = request.app.state.account_pool.get_status_summary()
    return {'status': 'ok' if status['active'] else 'degraded', 'accounts': status['active'],
            'accounts_total': status['total'], 'accounts_cooling': status['cooling'],
            'accounts_disabled': status.get('disabled', 0),
            'uptime': int(time.time() - request.app.state.start_time)}


frontend = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'frontend')
if os.path.isdir(frontend):
    app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')
