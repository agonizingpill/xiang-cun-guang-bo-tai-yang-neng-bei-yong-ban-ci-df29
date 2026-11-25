from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from core import auth
from core.db import metadata, transaction, engine
from core.config import SEED_DEMO
from core.middleware import install
from core.queueing import broker, view
from domain.routes import routers
from domain.battery_logs_service import RangeChanged
from scripts.seed_demo import seed


# 旧版本数据库的 battery_logs 可能缺少复核判定列；create_all 只建新表不改旧表，
# 这里做一次幂等的 ADD COLUMN，保证命名卷升级时也能运行。
_BATTERY_EXTRA_COLUMNS = {
    'verdict': 'VARCHAR(32)',
    'verdict_label': 'VARCHAR(120)',
    'predicted_wh': 'FLOAT',
    'band_wh': 'FLOAT',
    'deviation_wh': 'FLOAT',
    'direction': 'VARCHAR(8)',
    'segment_code': 'VARCHAR(64)',
    'range_token': 'VARCHAR(80)',
    'revision': 'INTEGER',
    'judged_by': 'VARCHAR(100)',
    'judged_at': 'VARCHAR(40)',
}


def upgrade_columns():
    from sqlalchemy import inspect

    inspector = inspect(engine)
    if 'battery_logs' not in inspector.get_table_names():
        return
    present = {col['name'] for col in inspector.get_columns('battery_logs')}
    with engine.begin() as conn:
        for name, ddl in _BATTERY_EXTRA_COLUMNS.items():
            if name not in present:
                conn.execute(text(f'ALTER TABLE battery_logs ADD COLUMN {name} {ddl}'))


@asynccontextmanager
async def lifespan(app):
    metadata.create_all(engine)
    upgrade_columns()
    if SEED_DEMO:
        try:
            seed()
        except Exception:
            logging.getLogger('uvicorn.error').exception('demo seed failed')
    yield

app = FastAPI(lifespan=lifespan)
install(app)


@app.exception_handler(RangeChanged)
async def range_changed_handler(request: Request, exc: RangeChanged):
    current = exc.current or {}
    body = {'detail': exc.detail, 'code': 'range_changed',
            'reason': 'pending_range_changed', 'recheck_required': True,
            'revision': current.get('revision', exc.revision),
            'version': current.get('version', exc.revision)}
    if exc.current is not None:
        body['current'] = exc.current
        body['pending_range'] = exc.current
        body['pending_segment'] = exc.current
        body['segment'] = exc.current
        body['shift'] = exc.current
        body['range_token'] = exc.current.get('range_token')
        body['review_token'] = exc.current.get('range_token')
        body['segment_token'] = exc.current.get('range_token')
        body['token'] = exc.current.get('range_token')
        body['meter_revision'] = exc.revision
    return JSONResponse(status_code=409, content=body)


app.include_router(auth.router)
for router in routers:
    app.include_router(router)

@app.get('/health')
def health():
    with transaction() as conn:
        conn.execute(text('SELECT 1'))
    return {'ok': bool(broker.ping())}

@app.get('/compute/{token}')
def compute(token: str, user=Depends(auth.actor)):
    return view(user, token)
