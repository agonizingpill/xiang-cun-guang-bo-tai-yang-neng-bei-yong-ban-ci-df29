"""抄数复核判定引擎（纯函数为主）。

业务规则（见任务说明）：
- 同一只电表有足够多的、更早的已审签读数时，用这些读数做 Fourier+OLS 预测；
- 实际用量落在历史误差带之外的抄数先挂起（不 verified）；
- 单次、孤立的超带判为尖峰（疑似抄错）；
- 连续至少三小时同向超带形成“待审换档段”，复核员确认换档后整段才审签；
- 晚到的抄数会重新参与连续性构造，可能补齐 / 扩展待审段；
- 待审段带内容令牌与电表复核版本，范围变化后旧令牌确认会被退回重审。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from domain.learning import design

# 至少有多少条更早的已审签读数才启动预测。取值保持宽松，
# 让“有足够历史”的演示 / 测试台账（通常 24~36 条以上）都能进入判定。
MIN_HISTORY = 8
MIN_HISTORY_HOURS = MIN_HISTORY
ENOUGH_HISTORY = MIN_HISTORY
# 形成换档怀疑所需的最少连续同向小时数（题目固定为至少三小时）。
SHIFT_HOURS = 3
MIN_RUN_HOURS = SHIFT_HOURS

# 判定代码
UNREVIEWED = 'unreviewed'
HISTORY_SHORT = 'history_short'
IN_BAND = 'in_band'
SPIKE = 'spike'
WATCH = 'watch'
SHIFT_PENDING = 'shift_pending'
SHIFT_CONFIRMED = 'shift_confirmed'
VERIFIED = 'verified'
BASELINE = 'baseline'

HELD_VERDICTS = (SPIKE, WATCH, SHIFT_PENDING)
ABNORMAL_VERDICTS = HELD_VERDICTS

VERDICT_LABELS = {
    UNREVIEWED: '待复核：已登记，尚未完成预测核对',
    HISTORY_SHORT: '历史不足：更早的已审签读数不够，按纸本基线审签',
    IN_BAND: '误差带内：用量符合历史预测',
    SPIKE: '尖峰挂起：单次抄数超出历史误差带，疑似抄错',
    WATCH: '挂起观察：连续两小时同向超出误差带，继续观察',
    SHIFT_PENDING: '待审换档段：连续{n}小时同向超出误差带，待复核员确认换档',
    SHIFT_CONFIRMED: '换档已确认：整段已审签，进入后续拟合',
    VERIFIED: '已审签',
    BASELINE: '历史基线：纸本审签',
}

DIRECTION_LABELS = {'up': '偏高', 'down': '偏低', 'flat': '符合', None: '—'}


def verdict_label(code: str | None, hours: int | None = None) -> str:
    if not code:
        return VERDICT_LABELS[UNREVIEWED]
    text = VERDICT_LABELS.get(code, code)
    if hours is not None:
        try:
            text = text.format(n=hours)
        except (KeyError, IndexError):
            pass
    return text


def predict_band(history: Iterable[dict], hour: int, clear: bool) -> dict[str, float]:
    """用更早的已审签读数拟合 Fourier 基 + OLS，给出预测与历史误差带半宽。"""
    rows = list(history)
    xs = [design(r['hour'], r['clear']) for r in rows]
    ys = [float(r['load_wh']) for r in rows]

    predicted: float
    mae: float
    sigma = 0.0
    try:
        from sklearn.linear_model import LinearRegression

        model = LinearRegression()
        model.fit(xs, ys)
        predicted = float(model.predict([design(hour, bool(clear))])[0])
        fitted = [float(v) for v in model.predict(xs)]
        residuals = [y - p for y, p in zip(ys, fitted)]
        mae = sum(abs(v) for v in residuals) / len(residuals)
        params = len(xs[0]) + 1  # 设计矩阵列 + 截距
        freedom = max(len(rows) - params, 1)
        sigma = (sum(v * v for v in residuals) / freedom) ** 0.5
    except Exception:
        # 极端数据下 OLS 退化时，回退到历史均值与平均绝对偏差。
        mean_y = sum(ys) / len(ys)
        predicted = mean_y
        residuals = [y - mean_y for y in ys]
        mae = sum(abs(v) for v in residuals) / len(residuals)
        sigma = (sum(v * v for v in residuals) / max(len(rows) - 1, 1)) ** 0.5

    predicted = max(predicted, 0.0)
    # 误差带 = 历史误差尺度（MAE / 残差标准差）与额定相对容差的较大者，
    # 常数台账（MAE≈0）时仍有合理带宽，避免把 88.4 这类轻微差异误挂起。
    relative_floor = 0.08 * max(predicted, 1.0)
    band = max(3.0 * mae, 2.0 * sigma, relative_floor, 4.0)
    return {'predicted_wh': round(predicted, 3),
            'band_wh': round(band, 3),
            'mae_wh': round(mae, 3),
            'sigma_wh': round(sigma, 3)}


def assess_row(row: dict, meter_rows: list[dict]) -> dict[str, Any]:
    """对单笔抄数做预测判定。只使用同表、更早、已审签的读数。"""
    hour = row['hour']
    history = [r for r in meter_rows
               if r['meter'] == row['meter'] and r.get('verified') and r['hour'] < hour]
    base = {'predicted_wh': None, 'band_wh': None, 'deviation_wh': None,
            'direction': None, 'enough_history': len(history) >= MIN_HISTORY,
            'history_count': len(history)}
    if len(history) < MIN_HISTORY:
        base['verdict'] = HISTORY_SHORT
        return base

    stats = predict_band(history, hour, row['clear'])
    predicted, band = stats['predicted_wh'], stats['band_wh']
    value = float(row['load_wh'])
    deviation = round(value - predicted, 3)
    if deviation > band:
        direction = 'up'
        verdict = IN_BAND  # 先给基础值，连续段在 classify_meter 中升级
        out_of_band = True
    elif deviation < -band:
        direction = 'down'
        out_of_band = True
    else:
        direction = 'flat'
        out_of_band = False
        verdict = IN_BAND
    base.update(stats)
    base['deviation_wh'] = deviation
    base['direction'] = direction
    base['out_of_band'] = out_of_band
    if out_of_band:
        base['verdict'] = 'out_of_band'
    else:
        base['verdict'] = IN_BAND
    return base


def range_token(meter: str, start_hour: int, end_hour: int, codes: list[str]) -> str:
    """待审范围的内容令牌：范围或成员一旦变化，令牌就变。"""
    raw = '|'.join([str(meter), str(start_hour), str(end_hour), ','.join(codes)])
    return 'rt_' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]


def token_version(token: str | None) -> int | None:
    """把内容令牌折成一个正整数版本号，方便使用整数 version 契约的复核脚本。

    它绑定范围内容而不是计数，因此即使晚到抄数绕过服务直接落库，
    范围一变版本也会变。
    """
    if not token:
        return None
    digest = token[3:] if token.startswith('rt_') else hashlib.sha1(token.encode()).hexdigest()
    try:
        return int(digest[:8], 16) % 2_000_000_000
    except ValueError:
        return int(hashlib.sha1(token.encode()).hexdigest()[:8], 16) % 2_000_000_000


def segment_code(meter: str, start_hour: int) -> str:
    digest = hashlib.sha1(str(meter).encode('utf-8')).hexdigest()[:10]
    return f'RS-{digest}-{start_hour:02d}'


def build_runs(meter_rows: list[dict], assessments: dict[str, dict] | None = None
               ) -> list[dict[str, Any]]:
    """构造同表、按小时连续、同向超带的游程。

    输入行可以是数据库行（含持久化 direction），也可以附带即时 assessments。
    只考虑尚未审签且超带的抄数。
    """
    ordered = sorted([r for r in meter_rows if not r.get('verified')],
                     key=lambda r: r['hour'])
    points: list[tuple[dict, str]] = []
    for r in ordered:
        assess = (assessments or {}).get(r['code'])
        if assess is not None:
            direction = assess.get('direction')
            out_of_band = bool(assess.get('out_of_band'))
        else:
            direction = r.get('direction')
            out_of_band = (direction in ('up', 'down')
                           and r.get('verdict') in ABNORMAL_VERDICTS + ('out_of_band',))
        if out_of_band and direction in ('up', 'down'):
            points.append((r, direction))

    runs: list[dict[str, Any]] = []
    current: list[dict] = []
    current_dir: str | None = None
    prev_hour: int | None = None

    def flush():
        if not current:
            return
        codes = [r['code'] for r in current]
        start, end = current[0]['hour'], current[-1]['hour']
        runs.append({
            'meter': current[0]['meter'],
            'start_hour': start,
            'end_hour': end,
            'hours': len(current),
            'direction': current_dir,
            'records': codes,
            'range_token': range_token(current[0]['meter'], start, end, codes),
            'code': segment_code(current[0]['meter'], start),
        })

    for r, direction in points:
        if current and direction == current_dir and r['hour'] == prev_hour + 1:
            current.append(r)
        else:
            flush()
            current = [r]
            current_dir = direction
        prev_hour = r['hour']
    flush()
    return runs


def classify_meter(meter_rows: list[dict]) -> dict[str, dict[str, Any]]:
    """返回每笔抄数的完整判定（不落库）。

    已审签行保留其持久化结论（如 shift_confirmed）；未审签行实时预测并
    按连续游程升级为 spike / watch / shift_pending。
    """
    rows = sorted(meter_rows, key=lambda r: r['hour'])
    assessments: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get('verified'):
            continue
        assessments[r['code']] = assess_row(r, rows)

    runs = build_runs(rows, assessments)
    run_by_code: dict[str, dict] = {}
    for run in runs:
        if run['hours'] >= 2:
            for code in run['records']:
                run_by_code[code] = run

    result: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get('verified'):
            code = r.get('verdict') or VERIFIED
            result[r['code']] = {
                'verdict': code,
                'label': r.get('verdict_label') or verdict_label(code),
                'direction': r.get('direction'),
                'predicted_wh': r.get('predicted_wh'),
                'band_wh': r.get('band_wh'),
                'deviation_wh': r.get('deviation_wh'),
                'segment_code': r.get('segment_code'),
                'range_token': r.get('range_token'),
                'run': None,
            }
            continue

        a = assessments[r['code']]
        run = run_by_code.get(r['code'])
        if a['verdict'] == HISTORY_SHORT:
            code = HISTORY_SHORT
        elif not a.get('out_of_band'):
            code = IN_BAND
        elif run is not None:
            if run['hours'] >= SHIFT_HOURS:
                code = SHIFT_PENDING
            else:
                code = WATCH
        else:
            code = SPIKE
        info = {
            'verdict': code,
            'label': verdict_label(code, run['hours'] if run else 1),
            'direction': a.get('direction'),
            'predicted_wh': a.get('predicted_wh'),
            'band_wh': a.get('band_wh'),
            'deviation_wh': a.get('deviation_wh'),
            'enough_history': a.get('enough_history'),
            'history_count': a.get('history_count'),
            'segment_code': run['code'] if run and run['hours'] >= SHIFT_HOURS else None,
            'range_token': run['range_token'] if run and run['hours'] >= SHIFT_HOURS else None,
            'run': run if run and run['hours'] >= SHIFT_HOURS else None,
        }
        result[r['code']] = info
    return result
