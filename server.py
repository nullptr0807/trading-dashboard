import os
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from api.trade import router as trade_router
from api.factors import router as factors_router
from api.factor_ai import router as factor_ai_router
from api.signal_quality import router as signal_quality_router
from api.backtest import router as backtest_router
from api.events import router as events_router
from api.intro import router as intro_router
from api.explore import router as explore_router
from api.frontier import router as frontier_router
from api.symbols import router as symbols_router
from api.factor_lab import router as factor_lab_router
from api.system_status import router as system_status_router
from api.live_account import router as live_account_router
from core.live_logging import get_live_logger, log_event


class SelectiveGZipMiddleware:
    """Compress ordinary responses without buffering the live SSE stream."""

    def __init__(self, app, minimum_size: int = 1024, compresslevel: int = 6):
        self.app = app
        self.gzip = GZipMiddleware(
            app, minimum_size=minimum_size, compresslevel=compresslevel,
        )

    async def __call__(self, scope, receive, send):
        if (
            scope.get('type') == 'http'
            and scope.get('path') == '/api/live-account/strategy/stream'
        ):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


app = FastAPI(
    title='Trading Dashboard', docs_url=None, redoc_url=None, openapi_url=None,
    default_response_class=ORJSONResponse,
)
_origins = [x.strip() for x in os.getenv(
    'DASHBOARD_CORS_ORIGINS',
    'https://www.gexinhub.com,https://gexinhub.com,http://127.0.0.1:8501,http://localhost:8501'
).split(',') if x.strip() and x.strip() != '*']
app.add_middleware(CORSMiddleware, allow_origins=_origins,
                   allow_methods=['GET', 'POST', 'PUT'],
                   allow_headers=['Content-Type', 'X-Moomoo-Read-Token',
                                  'X-Moomoo-Trade-Token', 'X-Moomoo-Control-Token'])
# Compress API/chart payloads at the application boundary.  A moderate minimum
# avoids spending CPU on tiny status responses while covering large JSON/JS.
app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024, compresslevel=6)
_live_api_logger = get_live_logger('live.dashboard.api', 'dashboard-api.jsonl')


@app.middleware('http')
async def live_api_access_log(request: Request, call_next):
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        status = 500
        raise
    finally:
        if request.url.path.startswith('/api/live-account'):
            log_event(_live_api_logger, 'info', 'live_account_api',
                      method=request.method, path=request.url.path, status=status,
                      latency_ms=round((time.monotonic() - started) * 1000, 2))
app.include_router(trade_router)
app.include_router(factors_router)
app.include_router(factor_ai_router)
app.include_router(signal_quality_router)
app.include_router(backtest_router)
app.include_router(events_router)
app.include_router(intro_router)
app.include_router(explore_router)
app.include_router(frontier_router)
app.include_router(symbols_router)
app.include_router(factor_lab_router)
app.include_router(system_status_router)
app.include_router(live_account_router)
class VersionedStaticFiles(StaticFiles):
    """Cache query-versioned assets forever; keep unversioned files revalidating."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        query = parse_qs(scope.get('query_string', b'').decode('latin-1'))
        if query.get('v'):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            response.headers['Cache-Control'] = 'no-cache'
        return response


app.mount('/static', VersionedStaticFiles(directory='static'), name='static')

@app.get('/')
async def index():
    return FileResponse('static/index.html', headers={'Cache-Control': 'no-cache'})
