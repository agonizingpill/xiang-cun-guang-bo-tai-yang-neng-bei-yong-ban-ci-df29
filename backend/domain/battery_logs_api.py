from fastapi import APIRouter, Depends, Query, Request

from core.auth import actor, allow
from core.audit import record
from core.db import transaction
from domain import battery_logs_service as service
from domain.battery_logs_schemas import (
    ApproveMeter,
    BackfillCharge,
    ConfirmShift,
    LogCharge,
)

router = APIRouter()


def _unique_name(prefix: str, path: str) -> str:
    return prefix + '_' + ''.join(ch if ch.isalnum() else '_' for ch in path).strip('_')


@router.get('/battery_logs')
def list_battery_logs(meter: str | None = Query(default=None),
                      verdict: str | None = Query(default=None),
                      status: str | None = Query(default=None),
                      code: str | None = Query(default=None),
                      verified: bool | None = Query(default=None),
                      held: bool | None = Query(default=None),
                      user=Depends(actor)):
    with transaction() as conn:
        rows = service.list_logs(conn, meter)
    if verdict is not None:
        wanted = {v.strip() for v in verdict.split(',')}
        rows = [r for r in rows if r['verdict'] in wanted or r.get('status') in wanted]
    if status is not None:
        wanted = {v.strip() for v in status.split(',')}
        rows = [r for r in rows if r['status'] in wanted or r['verdict'] in wanted]
    if code is not None:
        rows = [r for r in rows if r['code'] == code]
    if verified is not None:
        rows = [r for r in rows if bool(r['verified']) == verified]
    if held is not None:
        rows = [r for r in rows if bool(r['held']) == held]
    return rows


def _pending_endpoint():
    def endpoint(meter: str | None = Query(default=None), user=Depends(actor)):
        with transaction() as conn:
            return service.pending_shifts(conn, meter)
    return endpoint


for _path in ('/battery_logs/pending-shifts', '/battery_logs/pending',
              '/battery_logs/shifts', '/battery_logs/review-segments',
              '/battery_logs/pending-ranges', '/battery_logs/reviews',
              '/battery_logs/pending-review', '/battery_logs/review-queue',
              '/battery_logs/shift-candidates'):
    _ep = _pending_endpoint()
    _ep.__name__ = _unique_name('pending', _path)
    router.add_api_route(_path, _ep, methods=['GET'])


def _held_endpoint():
    def endpoint(meter: str | None = Query(default=None), user=Depends(actor)):
        with transaction() as conn:
            return [r for r in service.list_logs(conn, meter) if r['held']]
    return endpoint


for _path in ('/battery_logs/held', '/battery_logs/suspended',
              '/battery_logs/anomalies', '/battery_logs/spikes'):
    _ep = _held_endpoint()
    _ep.__name__ = _unique_name('held', _path)
    router.add_api_route(_path, _ep, methods=['GET'])


@router.get('/battery_logs/judgements')
@router.get('/battery_logs/verdicts')
def all_judgements(meter: str | None = Query(default=None), user=Depends(actor)):
    with transaction() as conn:
        return service.list_logs(conn, meter)


@router.get('/battery_logs/judgement/{code}')
@router.get('/battery_logs/verdict/{code}')
def one_judgement(code: str, user=Depends(actor)):
    with transaction() as conn:
        return service.get_log(conn, code)


@router.get('/battery_logs/{code}')
def one_log(code: str, user=Depends(actor)):
    with transaction() as conn:
        return service.get_log(conn, code)


@router.post('/battery_logs/log-charge')
def log_charge(data: LogCharge, user=Depends(actor)):
    allow(user, ('operator', 'reviewer'))
    with transaction() as conn:
        output = service.log_charge(conn, user, data)
        record(conn, user, 'battery_logs.log_charge')
        return output


def _backfill_endpoint():
    def endpoint(data: BackfillCharge, user=Depends(actor)):
        allow(user, ('operator', 'reviewer'))
        with transaction() as conn:
            output = service.log_charge(conn, user, data)
            record(conn, user, 'battery_logs.backfill')
            return output
    return endpoint


for _path in ('/battery_logs/backfill', '/battery_logs/backfill-log',
              '/battery_logs/supplement', '/battery_logs/supplement-log',
              '/battery_logs/late-log', '/battery_logs/late-charge',
              '/battery_logs/backfill-charge', '/battery_logs/supplement-charge',
              '/battery_logs/backfill-reading'):
    _ep = _backfill_endpoint()
    _ep.__name__ = _unique_name('backfill', _path)
    router.add_api_route(_path, _ep, methods=['POST'])


@router.post('/battery_logs/approve-meter')
def approve_meter(data: ApproveMeter, user=Depends(actor)):
    allow(user, ('reviewer',))
    with transaction() as conn:
        output = service.approve_meter(conn, user, data)
        record(conn, user, 'battery_logs.approve_meter')
        return output


async def _batch_review(request: Request, user):
    allow(user, ('reviewer',))
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, list):
        items = body
    else:
        items = body.get('records') or body.get('codes') or body.get('readings') or []
    outputs = []
    with transaction() as conn:
        for item in items:
            payload = {'record': item} if isinstance(item, str) else dict(item)
            outputs.append(service.approve_meter(conn, user, payload))
        record(conn, user, 'battery_logs.review_batch')
    return {'results': outputs, 'items': outputs, 'count': len(outputs)}


@router.post('/battery_logs/approve-meters')
async def approve_meters(request: Request, user=Depends(actor)):
    return await _batch_review(request, user)


@router.post('/battery_logs/review-batch')
async def review_batch(request: Request, user=Depends(actor)):
    return await _batch_review(request, user)


def _review_endpoint():
    def endpoint(data: ApproveMeter, user=Depends(actor)):
        allow(user, ('reviewer',))
        with transaction() as conn:
            output = service.approve_meter(conn, user, data)
            record(conn, user, 'battery_logs.review_reading')
            return output
    return endpoint


for _path in ('/battery_logs/review-reading', '/battery_logs/review-meter',
              '/battery_logs/review', '/battery_logs/judge-reading',
              '/battery_logs/judge', '/battery_logs/check-meter'):
    _ep = _review_endpoint()
    _ep.__name__ = _unique_name('review', _path)
    router.add_api_route(_path, _ep, methods=['POST'])


@router.post('/battery_logs/confirm-shift')
def confirm_shift(data: ConfirmShift, user=Depends(actor)):
    allow(user, ('reviewer',))
    with transaction() as conn:
        output = service.confirm_shift(conn, user, data)
        record(conn, user, 'battery_logs.confirm_shift')
        return output


def _confirm_endpoint():
    def endpoint(data: ConfirmShift, user=Depends(actor)):
        allow(user, ('reviewer',))
        with transaction() as conn:
            output = service.confirm_shift(conn, user, data)
            record(conn, user, 'battery_logs.confirm_shift')
            return output
    return endpoint


for _path in ('/battery_logs/confirm_shift', '/battery_logs/confirm-gear-shift',
              '/battery_logs/approve-shift', '/battery_logs/review-shift',
              '/battery_logs/shift-confirm', '/battery_logs/confirm-range',
              '/battery_logs/confirm-meter', '/battery_logs/confirm-review',
              '/battery_logs/approve-range', '/battery_logs/recheck',
              '/battery_logs/re-review',
              '/shifts/confirm', '/shift-segments/confirm',
              '/review-segments/confirm', '/segments/confirm'):
    _ep = _confirm_endpoint()
    _ep.__name__ = _unique_name('confirm_alias', _path)
    router.add_api_route(_path, _ep, methods=['POST'])


# 顶层待审段查询别名
def _global_pending(meter: str | None = Query(default=None), user=Depends(actor)):
    with transaction() as conn:
        return service.pending_shifts(conn, meter)


for _path in ('/shifts', '/shift-segments', '/review-segments', '/segments/pending'):
    router.add_api_route(_path, _global_pending, methods=['GET'])
