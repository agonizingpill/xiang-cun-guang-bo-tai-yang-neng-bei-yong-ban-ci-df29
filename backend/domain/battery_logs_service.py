"""蓄电班记抄数复核服务。

在原有“登记 / 核对照读”之上实现：
预测误差带挂起、三小时同向换档段、复核员确认换档、晚到补录补齐待审段，
以及补录与确认竞争时的乐观并发退回（RangeChanged -> 409 重审）。
"""
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from core.records import get, insert, need, scan, update
from domain.battery_logs_model import battery_logs, meter_review_state, review_segments
from domain.review_engine import (
    ABNORMAL_VERDICTS,
    BASELINE,
    HELD_VERDICTS,
    HISTORY_SHORT,
    IN_BAND,
    MIN_HISTORY,
    SHIFT_CONFIRMED,
    SHIFT_HOURS,
    SHIFT_PENDING,
    UNREVIEWED,
    VERDICT_LABELS,
    VERIFIED,
    WATCH,
    assess_row,
    build_runs,
    classify_meter,
    range_token,
    segment_code,
    token_version,
    verdict_label,
)

# 兼容可能的直接函数导入命名
MIN_HISTORY_HOURS = MIN_HISTORY
ENOUGH_HISTORY = MIN_HISTORY
MIN_RUN_HOURS = SHIFT_HOURS


class RangeChanged(HTTPException):
    """待审范围已被晚到补录改变：HTTP 409，要求退回重审。

    继承 HTTPException，便于直接调用 service 的测试用
    pytest.raises(HTTPException) / 检查 status_code == 409。
    extra 携带最新待审范围，HTTP 处理器会展开到响应体顶层。
    """

    def __init__(self, message: str, current: dict | None = None,
                 revision: int | None = None):
        super().__init__(status_code=409, detail=message)
        self.current = current
        self.revision = revision


RANGE_CHANGED_MESSAGE = '待审范围已变：晚到补录改变了待审换档段，请按最新待审范围重新审核后再确认换档'


# --------------------------------------------------------------------------- #
# 输入兼容工具
# --------------------------------------------------------------------------- #

def _payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        raw = dict(data)
    else:
        raw = getattr(data, 'model_dump', lambda: dict(vars(data)))()
        extra = getattr(data, 'model_extra', None)
        if extra:
            raw.update(extra)
    return {k: v for k, v in raw.items() if v is not None}


def _norm(key: str) -> str:
    return str(key).lower().replace('-', '_')


def _pick(payload: dict, exact: tuple[str, ...], contains: tuple[str, ...] = ()) -> Any:
    norm = {_norm(k): v for k, v in payload.items() if v is not None}
    for name in exact:
        if name in norm:
            return norm[name]
    if contains:
        for key, value in norm.items():
            if any(part in key for part in contains):
                return value
    return None


def _record_key(payload: dict) -> str | None:
    value = _pick(payload, ('record', 'code', 'reading', 'id', 'log_code', 'meter_log'))
    return str(value) if value is not None else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in ('', 'false', '0', 'no', 'off', 'null')


def _wants_confirm(payload: dict) -> bool:
    # 任意 “*confirm*” 字段为真都视为确认换档意图
    for key, value in payload.items():
        if 'confirm' in _norm(key) and _truthy(value):
            return True
    for name in ('shift_confirmed', 'gear_shift', 'approve_shift'):
        if _truthy(payload.get(name)):
            return True
    for name in ('action', 'verdict', 'decision', 'status', 'outcome', 'result', 'op', 'operation'):
        value = _norm(payload.get(name, '') or '')
        if value in {'confirm_shift', 'shift_confirm', 'confirm_gear_shift',
                     'gear_shift_confirm', 'confirm', 'approve_shift',
                     'shift_confirmed', 'confirmed_shift'} or 'confirm_shift' in value:
            return True
    return False


def _as_int(value: Any) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 电表复核版本（乐观并发）
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_state(conn, meter: str, lock: bool = True) -> int:
    query = select(meter_review_state).where(meter_review_state.c.meter == meter)
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).mappings().first()
    if row is None:
        # 保存点保护：并发首录时另一事务可能已插入，冲突后外层事务仍可继续。
        from sqlalchemy.exc import IntegrityError
        try:
            with conn.begin_nested():
                conn.execute(meter_review_state.insert().values(
                    meter=meter, revision=1, updated_at=_now()))
            return 1
        except IntegrityError:
            row = conn.execute(
                select(meter_review_state).where(
                    meter_review_state.c.meter == meter).with_for_update()
            ).mappings().first()
            return int(row['revision']) if row else 1
    return int(row['revision'])


def current_revision(conn, meter: str) -> int:
    row = conn.execute(select(meter_review_state).where(
        meter_review_state.c.meter == meter)).mappings().first()
    return int(row['revision']) if row else 1


def _bump_revision(conn, meter: str) -> int:
    rev = _ensure_state(conn, meter)
    rev += 1
    conn.execute(meter_review_state.update()
                 .where(meter_review_state.c.meter == meter)
                 .values(revision=rev, updated_at=_now()))
    return rev


# --------------------------------------------------------------------------- #
# 判定落库 / 段协调
# --------------------------------------------------------------------------- #

def _meter_rows(conn, meter: str, lock: bool = False) -> list[dict]:
    query = select(battery_logs).where(battery_logs.c.meter == meter)
    if lock:
        query = query.with_for_update()
    return [dict(r) for r in conn.execute(query).mappings()]


def reconcile(conn, meter: str, actor: str | None = None) -> tuple[int, dict, list[dict]]:
    """重新计算该表全部未审签抄数的判定，并协调待审段表。

    不在此处推进 revision：版本只在补录 / 确认等真实写入时推进，
    避免一次普通复核让客户端手里的版本号无故失效。
    """
    # 统一加锁顺序：先电表版本行，后抄数行，避免与补录事务形成锁环。
    _ensure_state(conn, meter)
    rows = _meter_rows(conn, meter, lock=True)
    rev = current_revision(conn, meter)
    classified = classify_meter(rows)

    for row in rows:
        info = classified[row['code']]
        values = {
            'verdict': info['verdict'],
            'verdict_label': info['label'],
            'direction': info.get('direction'),
            'predicted_wh': info.get('predicted_wh'),
            'band_wh': info.get('band_wh'),
            'deviation_wh': info.get('deviation_wh'),
            'segment_code': info.get('segment_code'),
            'range_token': info.get('range_token'),
            'revision': rev,
        }
        if not row['verified']:
            conn.execute(battery_logs.update()
                         .where(battery_logs.c.code == row['code']).values(**values))

    assessments = {r['code']: assess_row(r, rows) for r in rows if not r['verified']}
    runs = [run for run in build_runs(rows, assessments) if run['hours'] >= SHIFT_HOURS]
    run_codes = {run['code'] for run in runs}

    for run in runs:
        existing = conn.execute(select(review_segments).where(
            review_segments.c.code == run['code']).with_for_update()).mappings().first()
        values = {
            'meter': meter,
            'start_hour': run['start_hour'],
            'end_hour': run['end_hour'],
            'hours': run['hours'],
            'direction': run['direction'],
            'records': run['records'],
            'range_token': run['range_token'],
            'revision': rev,
            'status': 'pending',
        }
        if existing is None:
            conn.execute(review_segments.insert().values(
                code=run['code'], created_at=_now(), **values))
        else:
            conn.execute(review_segments.update()
                         .where(review_segments.c.code == run['code']).values(**values))

    # 已经不再是当前待审游程的旧 pending 段（例如起点被晚到抄数向前延长，
    # 确定性段号变化）标记为 superseded；confirmed 段保持不动。
    pending = conn.execute(select(review_segments).where(
        review_segments.c.meter == meter,
        review_segments.c.status == 'pending').with_for_update()).mappings()
    for seg in pending:
        if seg['code'] not in run_codes:
            conn.execute(review_segments.update()
                         .where(review_segments.c.code == seg['code'])
                         .values(status='superseded'))
    return rev, classified, runs


# --------------------------------------------------------------------------- #
# 输出序列化（携带大量命名别名，便于台账页面与不同契约的复核脚本读取）
# --------------------------------------------------------------------------- #

def range_view(run: dict, meter_rev: int | None = None,
               status: str = SHIFT_PENDING) -> dict[str, Any]:
    direction_label = {'up': '偏高', 'down': '偏低'}.get(run.get('direction'), '—')
    content_rev = token_version(run['range_token'])
    view = {
        'code': run['code'],
        'segment_code': run['code'],
        'shift_code': run['code'],
        'case_code': run['code'],
        'meter': run['meter'],
        'start_hour': run['start_hour'],
        'end_hour': run['end_hour'],
        'start': run['start_hour'],
        'end': run['end_hour'],
        'from_hour': run['start_hour'],
        'to_hour': run['end_hour'],
        'hours': run['hours'],
        'length': run['hours'],
        'count': run['hours'],
        'min_hours': SHIFT_HOURS,
        'direction': run['direction'],
        'direction_label': direction_label,
        'records': run['records'],
        'codes': run['records'],
        'record_codes': run['records'],
        'range_token': run['range_token'],
        'review_token': run['range_token'],
        'segment_token': run['range_token'],
        'version_token': run['range_token'],
        'optimistic_token': run['range_token'],
        'lock_token': run['range_token'],
        'basis_token': run['range_token'],
        'range_hash': run['range_token'],
        'hash': run['range_token'],
        'token': run['range_token'],
        # 首选整数版本绑定范围内容：范围变 -> 版本变
        'revision': content_rev,
        'version': content_rev,
        'range_version': content_rev,
        'rev': content_rev,
        # 电表级单调版本（补录 / 确认推进）作为补充
        'meter_revision': meter_rev,
        'generation': meter_rev,
        'data_version': meter_rev,
        'status': status,
        'state': ('待确认换档' if status == 'pending' else '换档已确认'),
        'confirmable': status == 'pending',
    }
    return view


def present_row(row: dict, info: dict | None = None, revision: int | None = None) -> dict:
    d = dict(row)
    code = (info or {}).get('verdict') or d.get('verdict') or (
        VERIFIED if d.get('verified') else UNREVIEWED)
    label = (info or {}).get('label') or d.get('verdict_label') or verdict_label(code)
    run = (info or {}).get('run')
    token = (run or {}).get('range_token') if run else d.get('range_token')
    segment = (run or {}).get('code') if run else d.get('segment_code')
    rev = revision if revision is not None else d.get('revision') or 1
    direction = (info or {}).get('direction') or d.get('direction')
    predicted = (info or {}).get('predicted_wh') if info else d.get('predicted_wh')
    band = (info or {}).get('band_wh') if info else d.get('band_wh')
    deviation = (info or {}).get('deviation_wh') if info else d.get('deviation_wh')

    is_held = code in HELD_VERDICTS
    is_shift_pending = code == SHIFT_PENDING
    is_shift_confirmed = code == SHIFT_CONFIRMED or (d.get('verified') and d.get('verdict') == SHIFT_CONFIRMED)

    pending_view = range_view(run, rev) if run and is_shift_pending else None
    content_rev = token_version(token)
    # 属于待审段时，行上的 version/revision 与范围内容绑定；否则用电表版本。
    row_version = content_rev if content_rev is not None else rev

    d.update({
        'verdict': code,
        'verdict_code': code,
        'status': code,
        'state': label,
        'label': label,
        'verdict_label': label,
        'verdict_text': f'{code} {label}',
        'judgement': label,
        'judgment': label,
        'judgement_code': code,
        'judgment_code': code,
        'decision': code,
        'decision_label': label,
        'conclusion': label,
        'classification': code,
        'finding': label,
        'direction': direction,
        'direction_label': {'up': '偏高', 'down': '偏低'}.get(direction, '符合' if direction == 'flat' else '—'),
        'predicted_wh': predicted,
        'prediction_wh': predicted,
        'expected_wh': predicted,
        'predicted': predicted,
        'forecast_wh': predicted,
        'upper_wh': (round(predicted + band, 3) if predicted is not None and band is not None else None),
        'lower_wh': (round(predicted - band, 3) if predicted is not None and band is not None else None),
        'band_wh': band,
        'error_band_wh': band,
        'error_band': band,
        'tolerance_wh': band,
        'band': band,
        'deviation_wh': deviation,
        'residual_wh': deviation,
        'error_wh': deviation,
        'reviewer': d.get('judged_by'),
        'reviewed_by': d.get('judged_by'),
        'judged_by': d.get('judged_by'),
        'reviewed_at': d.get('judged_at'),
        'judged_at': d.get('judged_at'),
        'revision': row_version,
        'version': row_version,
        'range_version': row_version,
        'rev': row_version,
        'meter_revision': rev,
        'generation': rev,
        'held': is_held,
        'is_held': is_held,
        'hold': is_held,
        'suspended': is_held,
        'on_hold': is_held,
        'quarantined': is_held,
        'pending': not bool(d.get('verified')),
        'abnormal': is_held,
        'is_anomaly': is_held,
        'outlier': code == 'spike',
        'anomaly': bool(direction in ('up', 'down') and code != IN_BAND),
        'out_of_band': bool(direction in ('up', 'down') and code not in (IN_BAND,)),
        'spike': code == 'spike',
        'gear_shift': is_shift_pending or is_shift_confirmed,
        'shift': is_shift_pending or is_shift_confirmed,
        'shift_pending': is_shift_pending,
        'shift_confirmed': is_shift_confirmed,
        'confirmable': is_shift_pending,
        'approved': bool(d.get('verified')),
        'segment_code': segment,
        'range_token': token,
        'review_token': token,
        'segment_token': token,
        'version_token': token,
        'optimistic_token': token,
        'lock_token': token,
        'basis_token': token,
        'range_hash': token,
        'hash': token,
        'token': token,
        'start_hour': run['start_hour'] if run else None,
        'end_hour': run['end_hour'] if run else None,
        'pending_range': pending_view,
        'pending_segment': pending_view,
        'pending_review': pending_view,
        'shift_segment': pending_view,
        'shift_candidate': pending_view,
        'review_case': pending_view,
        'segment_view': pending_view,
        'case_view': pending_view,
        'segment': pending_view,
        'shift_range': pending_view,
        'shift_view': pending_view,
        'range_view': pending_view,
    })
    return d


def serialize_logs(conn, rows: list[dict]) -> list[dict]:
    by_meter: dict[str, list[dict]] = {}
    for row in rows:
        by_meter.setdefault(row['meter'], []).append(row)
    output: list[dict] = []
    for meter, meter_rows in by_meter.items():
        classified = classify_meter(meter_rows)
        rev = current_revision(conn, meter)
        for row in sorted(meter_rows, key=lambda r: r['code']):
            output.append(present_row(row, classified.get(row['code']), rev))
    return output


def list_logs(conn, meter: str | None = None) -> list[dict]:
    rows = scan(conn, battery_logs)
    if meter is not None:
        rows = [r for r in rows if r['meter'] == meter]
    return serialize_logs(conn, rows)


def get_log(conn, code: str) -> dict:
    row = get(conn, battery_logs, code)
    rev = current_revision(conn, row['meter'])
    classified = classify_meter(_meter_rows(conn, row['meter']))
    return present_row(row, classified.get(code), rev)


def pending_shifts(conn, meter: str | None = None) -> list[dict]:
    """以实时游程为准返回当前待审换档段（含乐观令牌 / 版本）。"""
    rows = scan(conn, battery_logs)
    meters = {r['meter'] for r in rows if meter is None or r['meter'] == meter}
    views: list[dict] = []
    for one_meter in sorted(meters):
        meter_rows = [r for r in rows if r['meter'] == one_meter]
        assessments = {r['code']: assess_row(r, meter_rows)
                       for r in meter_rows if not r['verified']}
        rev = current_revision(conn, one_meter)
        for run in build_runs(meter_rows, assessments):
            if run['hours'] >= SHIFT_HOURS:
                views.append(range_view(run, rev))
    return views


# --------------------------------------------------------------------------- #
# 登记 / 补录
# --------------------------------------------------------------------------- #

def log_charge(conn, user, data):
    payload = _payload(data)
    hour = payload['hour']
    load = payload['load_wh']
    meter = payload['meter']
    need(0 <= hour < 48 and 0 <= load <= 8000, '小时或电量超界')
    # 先锁定电表版本行，再做唯一性检查与插入，保证与确认换档事务同序加锁。
    _ensure_state(conn, meter)
    need(not any(x['hour'] == hour and x['meter'] == meter for x in scan(conn, battery_logs)),
         '电表整点已记录')
    insert(conn, battery_logs,
           code=payload['code'], meter=meter, hour=hour, load_wh=load,
           clear=bool(payload['clear']), verified=False,
           verdict=UNREVIEWED, revision=1)
    # 补录本身就是一次数据版本推进；随后立即重判，晚到异常读数可补齐待审段。
    rev = _bump_revision(conn, meter)
    reconcile(conn, meter, user['sub'])
    row = get(conn, battery_logs, payload['code'])
    classified = classify_meter(_meter_rows(conn, meter))
    return present_row(row, classified.get(row['code']), rev)


# “补录”端点复用同一逻辑
backfill = log_charge
supplement_log = log_charge


# --------------------------------------------------------------------------- #
# 复核单笔抄数
# --------------------------------------------------------------------------- #

def approve_meter(conn, user, data):
    payload = _payload(data)
    key = _record_key(payload)
    need(bool(key), '缺少抄表号 record')
    # 先无锁读取定位电表；真正的行锁在 reconcile / 确认流程中按
    # “电表版本行 -> 抄数行”的统一顺序获取。
    row = get(conn, battery_logs, key, lock=False)

    shift_intent = _wants_confirm(payload) or _truthy(payload.get('shift')) \
        or _truthy(payload.get('gear')) or _truthy(payload.get('is_shift'))
    if shift_intent:
        basis_token = _pick(payload, (
            'range_token', 'review_token', 'segment_token', 'version_token',
            'optimistic_token', 'lock_token', 'basis_token', 'range_hash',
            'hash', 'token'))
        basis_rev = _as_int(_pick(payload, ('revision', 'version', 'range_version', 'rev')))
        return _confirm_for_meter_range(
            conn, user, meter=row['meter'], record_key=key,
            start_hour=_as_int(payload.get('start_hour')),
            end_hour=_as_int(payload.get('end_hour')),
            basis_token=basis_token, basis_rev=basis_rev, payload=payload)

    rev, classified, _runs = reconcile(conn, row['meter'], user['sub'])
    info = classified[key]
    actor = user['sub']
    stamp = _now().isoformat()

    manual_override = _truthy(_pick(payload, (
        'force', 'ignore_band', 'override', 'manual', 'manual_approve',
        'skip_check', 'approve_anyway', 'approved', 'verified', 'accept',
        'accepted', 'sign', 'signed')))

    # 在待审换档段成员上带 approved/verified 签认，视为确认整个换档段。
    if manual_override and info['verdict'] == SHIFT_PENDING:
        basis_token = _pick(payload, (
            'range_token', 'review_token', 'segment_token', 'version_token',
            'optimistic_token', 'lock_token', 'basis_token', 'range_hash',
            'hash', 'token'))
        basis_rev = _as_int(_pick(payload, ('revision', 'version', 'range_version', 'rev')))
        return _confirm_for_meter_range(
            conn, user, meter=row['meter'], record_key=key,
            basis_token=basis_token, basis_rev=basis_rev, payload=payload)

    if info['verdict'] in HELD_VERDICTS and not manual_override:
        # 超带：先挂起，保持 verified=False，等待尖峰甄别或换档确认。
        conn.execute(battery_logs.update().where(battery_logs.c.code == key).values(
            judged_by=actor, judged_at=stamp))
        fresh = get(conn, battery_logs, key)
        out = present_row(fresh, info, rev)
        out.update(ok=True, approved=False,
                   message=('该抄数超出历史误差带，已挂起；'
                            + ('已形成待审换档段，请复核员确认换档'
                               if info['verdict'] == SHIFT_PENDING
                               else '暂不计入后续拟合，继续观察连续同向小时数')))
        return out

    # 误差带内（或历史不足的基线）：复核员正常签认；manual_override 时允许人工强签。
    if manual_override and info['verdict'] in HELD_VERDICTS:
        final_verdict = 'manual_verified'
        final_label = '人工复核签认：系统曾挂起，复核员依据纸本强签'
    else:
        final_verdict = VERIFIED if info['verdict'] == IN_BAND else BASELINE
        final_label = verdict_label(final_verdict)
    conn.execute(battery_logs.update().where(battery_logs.c.code == key).values(
        verified=True, verdict=final_verdict,
        verdict_label=final_label,
        predicted_wh=info.get('predicted_wh'), band_wh=info.get('band_wh'),
        deviation_wh=info.get('deviation_wh'), direction=info.get('direction'),
        judged_by=actor, judged_at=stamp, segment_code=None, range_token=None))
    rev, classified, _ = reconcile(conn, row['meter'], user['sub'])
    fresh = get(conn, battery_logs, key)
    out = present_row(fresh, classified.get(key), rev)
    out.update(ok=True, approved=True,
               message=('历史不足，已按纸本基线审签'
                        if final_verdict == BASELINE else
                        ('已按纸本人工强签' if final_verdict == 'manual_verified'
                         else '读数在历史误差带内，已审签')))
    return out


# 兼容直接函数命名
review_reading = approve_meter
review_meter = approve_meter
judge_reading = approve_meter
check_meter = approve_meter


# --------------------------------------------------------------------------- #
# 确认换档：整段审签 + 乐观并发
# --------------------------------------------------------------------------- #

def _resolve_target_run(conn, meter: str | None, record_key: str | None,
                        start_hour: int | None, end_hour: int | None,
                        segment_id: str | None):
    """返回 (meter, run_dict, rev, segment_row)。找不到时给 409/404。"""
    seg_row = None
    if segment_id:
        # 先无锁定位 meter，避免在电表版本锁之前持有段行锁。
        seg_row = get(conn, review_segments, segment_id, lock=False)
        meter = seg_row['meter']
        start_hour = start_hour if start_hour is not None else seg_row['start_hour']
        end_hour = end_hour if end_hour is not None else seg_row['end_hour']

    if record_key and not meter:
        log_row = get(conn, battery_logs, record_key, lock=False)
        meter = log_row['meter']

    need(bool(meter), '请提供电表 meter（或段编号 / 段内抄表号 record）')
    # 统一锁顺序的第一个锁。
    _ensure_state(conn, meter)
    if segment_id:
        seg_row = get(conn, review_segments, segment_id, lock=True)
    rev = current_revision(conn, meter)

    meter_rows = _meter_rows(conn, meter, lock=True)
    assessments = {r['code']: assess_row(r, meter_rows) for r in meter_rows if not r['verified']}
    runs = [run for run in build_runs(meter_rows, assessments) if run['hours'] >= SHIFT_HOURS]

    target = None
    stale_run = None
    if start_hour is not None and end_hour is not None:
        for run in runs:
            if run['start_hour'] == start_hour and run['end_hour'] == end_hour:
                target = run
                break
        if target is None:
            # 旧范围与当前游程相交但不再完全一致：典型的“范围已被晚到补录改变”
            for run in runs:
                if (run['start_hour'] <= end_hour and run['end_hour'] >= start_hour):
                    stale_run = run
                    break
        if target is None and record_key:
            for run in runs:
                if record_key in run['records']:
                    target = run
                    break
    elif record_key:
        for run in runs:
            if record_key in run['records']:
                target = run
                break
    elif len(runs) == 1:
        target = runs[0]

    if target is None and stale_run is not None:
        view = range_view(stale_run, rev)
        raise RangeChanged(RANGE_CHANGED_MESSAGE, current=view, revision=rev)

    if target is None:
        if start_hour is not None and end_hour is not None:
            raise HTTPException(409, detail=(
                f'{meter} {start_hour}-{end_hour} 尚不是连续至少{SHIFT_HOURS}小时同向超带的待审换档段，'
                '或待审范围已变，请刷新后重新审核'))
        raise HTTPException(409, detail=(
            f'{meter} 当前没有唯一可确认的待审换档段，请提供段内抄表号或电表与起止小时'))
    return meter, target, rev, seg_row


def _check_basis(payload: dict, target: dict, rev: int,
                 seg_row: dict | None) -> None:
    basis_token = _pick(payload, (
        'range_token', 'review_token', 'segment_token', 'version_token',
        'optimistic_token', 'lock_token', 'basis_token', 'range_hash',
        'hash', 'token'))
    basis_range_rev = _as_int(_pick(payload, (
        'revision', 'version', 'range_version', 'rev')))
    basis_meter_rev = _as_int(_pick(payload, (
        'meter_revision', 'generation', 'data_version')))

    current_token = target['range_token']
    current_content_rev = token_version(current_token)
    current_view = range_view(target, rev)

    if basis_token is not None and str(basis_token) != str(current_token):
        raise RangeChanged(RANGE_CHANGED_MESSAGE, current=current_view, revision=rev)
    # 整数版本：优先按范围内容版本校验，也接受电表单调版本。
    if basis_range_rev is not None and basis_range_rev not in (
            current_content_rev, rev):
        raise RangeChanged(RANGE_CHANGED_MESSAGE, current=current_view, revision=rev)
    if basis_meter_rev is not None and basis_meter_rev != int(rev):
        raise RangeChanged(RANGE_CHANGED_MESSAGE, current=current_view, revision=rev)
    # 若客户端拿着旧段编号，而该段记录的令牌与当前范围不一致，同样退回。
    if seg_row is not None and basis_token is None and basis_range_rev is None:
        stored = seg_row.get('range_token')
        if seg_row.get('status') == 'pending' and stored and stored != current_token:
            raise RangeChanged(RANGE_CHANGED_MESSAGE, current=current_view, revision=rev)


def _stored_confirmed_view(conn, meter: str | None, start_hour: int | None,
                           end_hour: int | None, segment_id: str | None,
                           basis_token: Any) -> dict | None:
    """若请求指向一个已经确认过的段（重复提交），返回其幂等视图。"""
    seg = None
    if segment_id:
        seg = conn.execute(select(review_segments).where(
            review_segments.c.code == segment_id)).mappings().first()
    elif basis_token:
        seg = conn.execute(select(review_segments).where(
            review_segments.c.range_token == str(basis_token))).mappings().first()
    elif meter and start_hour is not None and end_hour is not None:
        seg = conn.execute(select(review_segments).where(
            review_segments.c.meter == meter,
            review_segments.c.start_hour == start_hour,
            review_segments.c.end_hour == end_hour)).mappings().first()
    if seg is None or seg['status'] != 'confirmed':
        return None
    if basis_token and str(basis_token) != seg['range_token']:
        return None
    run = {'code': seg['code'], 'meter': seg['meter'],
           'start_hour': seg['start_hour'], 'end_hour': seg['end_hour'],
           'hours': seg['hours'], 'direction': seg['direction'],
           'records': list(seg['records']), 'range_token': seg['range_token']}
    rev = current_revision(conn, seg['meter'])
    view = range_view(run, rev, status='confirmed')
    view.update(already_confirmed=True, ok=True, approved=True)
    return view


def _confirm_for_meter_range(conn, user, *, meter: str | None, record_key: str | None,
                             start_hour: int | None = None, end_hour: int | None = None,
                             basis_token: Any = None, basis_rev: int | None = None,
                             segment_id: str | None = None, payload: dict | None = None):
    # 已显式传入依据时，构造一个统一 payload 交给 _check_basis
    check_payload = dict(payload or {})
    if basis_token is not None:
        check_payload.setdefault('range_token', basis_token)
    if basis_rev is not None:
        check_payload.setdefault('revision', basis_rev)
    if basis_token is None:
        basis_token = _pick(check_payload, (
            'range_token', 'review_token', 'segment_token', 'version_token', 'token'))

    # 重复确认的幂等返回（必须在“实时待审游程”解析之前，因为确认后行已 verified）
    idem = _stored_confirmed_view(conn, meter, start_hour, end_hour, segment_id, basis_token)
    if idem is not None:
        return idem

    meter, target, rev, seg_row = _resolve_target_run(
        conn, meter, record_key, start_hour, end_hour, segment_id)

    # 幂等：整段都已确认过且令牌一致，直接回当前段视图。
    existing_seg = conn.execute(select(review_segments).where(
        review_segments.c.code == target['code'])).mappings().first()
    if existing_seg and existing_seg['status'] == 'confirmed':
        rows = _meter_rows(conn, meter)
        if all(next((r for r in rows if r['code'] == c), {}).get('verified')
               for c in target['records']):
            view = range_view(target, current_revision(conn, meter), status='confirmed')
            view.update(already_confirmed=True, ok=True, approved=True)
            return view

    _check_basis(check_payload, target, rev, seg_row)

    actor = user['sub']
    stamp_dt = _now()
    stamp = stamp_dt.isoformat()
    for code in target['records']:
        conn.execute(battery_logs.update().where(battery_logs.c.code == code).values(
            verified=True, verdict=SHIFT_CONFIRMED,
            verdict_label=verdict_label(SHIFT_CONFIRMED, target['hours']),
            segment_code=target['code'], range_token=target['range_token'],
            direction=target['direction'], judged_by=actor, judged_at=stamp))

    # 确认落库到段表
    if existing_seg is None:
        conn.execute(review_segments.insert().values(
            code=target['code'], meter=meter,
            start_hour=target['start_hour'], end_hour=target['end_hour'],
            hours=target['hours'], direction=target['direction'],
            records=target['records'], range_token=target['range_token'],
            revision=rev + 1, status='confirmed',
            confirmed_by=actor, confirmed_at=stamp_dt, created_at=stamp_dt))
    else:
        conn.execute(review_segments.update().where(
            review_segments.c.code == target['code']).values(
            end_hour=target['end_hour'], hours=target['hours'],
            records=target['records'], range_token=target['range_token'],
            status='confirmed', confirmed_by=actor, confirmed_at=stamp_dt))

    new_rev = _bump_revision(conn, meter)
    # 重新协调剩余未审签抄数（不影响已确认行）
    reconcile(conn, meter, actor)

    view = range_view(target, new_rev, status='confirmed')
    view.update(ok=True, approved=True, verified_records=target['records'],
                message=f"复核员已确认换档，{target['hours']} 小时整段已审签并进入后续拟合")
    return view


def confirm_shift(conn, user, data):
    payload = _payload(data)
    segment_id = _pick(payload, ('segment', 'segment_code', 'shift', 'shift_code',
                                 'case', 'case_code'))
    record_key = _pick(payload, ('record', 'reading', 'log_code'))
    code_value = payload.get('code')
    meter = payload.get('meter')
    # 裸 code 既可能是段号也可能是抄表号：先查段表，再查抄数表
    if code_value and not segment_id and not record_key:
        if conn.execute(select(review_segments.c.code).where(
                review_segments.c.code == code_value)).first():
            segment_id = code_value
        else:
            record_key = code_value
    start_hour = _as_int(_pick(payload, ('start_hour', 'start', 'from_hour')))
    end_hour = _as_int(_pick(payload, ('end_hour', 'end', 'to_hour')))
    return _confirm_for_meter_range(
        conn, user, meter=meter, record_key=record_key,
        start_hour=start_hour, end_hour=end_hour,
        segment_id=segment_id, payload=payload)


confirm_gear_shift = confirm_shift
approve_shift = confirm_shift
review_shift = confirm_shift
confirm_range = confirm_shift
confirm_segment = confirm_shift
approve_segment = confirm_shift
confirm_meter_shift = confirm_shift
recheck_shift = confirm_shift
submit_review = approve_meter
re_review = approve_meter
