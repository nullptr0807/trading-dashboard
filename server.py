from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from api.trade import router as trade_router
from api.factors import router as factors_router
from api.backtest import router as backtest_router
from api.events import router as events_router
from api.intro import router as intro_router
from api.explore import router as explore_router
from api.symbols import router as symbols_router

app = FastAPI(title='Trading Dashboard', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
app.include_router(trade_router)
app.include_router(factors_router)
app.include_router(backtest_router)
app.include_router(events_router)
app.include_router(intro_router)
app.include_router(explore_router)
app.include_router(symbols_router)
app.mount('/static', StaticFiles(directory='static'), name='static')

@app.get('/')
async def index():
    return FileResponse('static/index.html')
