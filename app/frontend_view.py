"""Serve the existing UI template with one shared compatibility layer.

The large HTML template is preserved. Only boot-time storage access and script
loading are adapted here; behavior fixes live in frontend/app-fixes.js. Both /
and /index.html must use this renderer, not the raw static template.
"""
from functools import lru_cache
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def render_index():
    source = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
    marker = '<script>\n/* ===== NAMESPACE ===== */'
    if source.count(marker) != 1:
        raise RuntimeError('UI template changed; update frontend_view before deployment.')
    source = source.replace(marker, '<script src="/safe-browser.js"></script>\n<script src="/stream-reader.js"></script>\n' + marker)
    source = source.replace('localStorage.', 'SafeBrowserStorage.local.').replace('sessionStorage.', 'SafeBrowserStorage.session.')
    source = source.replace("JSON.parse(SafeBrowserStorage.local.getItem('claude_chats'))||[]", 'SafeBrowserStorage.loadChats()')
    source = source.replace('https://cdn.jsdelivr.net/npm/marked/marked.min.js', 'https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js')
    source = source.replace('</body>', '<script src="/app-fixes.js"></script>\n</body>')
    return source


@router.get('/', response_class=HTMLResponse, include_in_schema=False)
@router.get('/index.html', response_class=HTMLResponse, include_in_schema=False)
async def index():
    return HTMLResponse(render_index(), headers={'Cache-Control': 'no-cache'})
