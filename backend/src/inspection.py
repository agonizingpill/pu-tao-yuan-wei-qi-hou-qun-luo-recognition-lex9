"""Whole-orchard shift baselines and inspection orders.

Every sensor is judged against its own previous seven days of same-shift readings.
The common offset of the whole orchard for that shift is subtracted before a
residual may open an inspection order.  Missing or thin baselines wait for
supplementary collection instead of raising an order.
"""

import hashlib
import json
from datetime import date, timedelta
from statistics import median

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .db import event
from .domain import InspectionState, ShiftState, Slot

BASELINE_DAYS = 7
MIN_BASELINE = 3
MIN_PEERS = 3
SIGMA = 3.0
TEMP_FLOOR = 1.5
RH_FLOOR = 8.0

ORDER_MESSAGES = {
    'accepted': '该巡检单已接手，以先入账者为准',
    'pending_review': '该巡检单已接手，正在等待复核',
    'revoked': '该巡检单已撤销，不能再接单',
}


def _one(db, sql, params, what):
    row = db.execute(sql, params).fetchone()
    if row is None:
        raise HTTPException(404, f'{what}不存在')
    return row


def _round(value, digits=3):
    return None if value is None else round(float(value), digits)


def _stats(values):
    if not values:
        return {'median': None, 'mad': None, 'sigma': None, 'n': 0}
    center = median(values)
    spread = median([abs(value - center) for value in values])
    return {'median': center, 'mad': spread, 'sigma': 1.4826 * spread, 'n': len(values)}


def _baseline_json(stats, samples):
    def summarise(stats):
        return {'median': _round(stats['median']), 'mad': _round(stats['mad']),
                'sigma': _round(stats['sigma']), 'n': stats['n']}
    return {
        'temp_c': summarise(stats['temp_c']),
        'rh_pct': summarise(stats['rh_pct']),
        'samples': [{'observed_day': row['observed_day'].isoformat(), 'temp_c': _round(row['temp_c']),
                     'rh_pct': _round(row['rh_pct']), 'source': row['source']} for row in samples],
    }


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _threshold(stats, floor):
    if not stats['n']:
        return None
    return max(SIGMA * stats['sigma'], floor)


def _evaluate_block(block, day, slot, observed, readings, peers):
    """Build the immutable judgment for one sensor and one shift."""
    baseline = {'temp_c': _stats([row['temp_c'] for row in readings]),
                'rh_pct': _stats([row['rh_pct'] for row in readings])}
    judgment = {
        'observed_day': day.isoformat(),
        'slot': slot.value,
        'block_id': block['id'],
        'block_code': block['code'],
        'sensor': block['sensor'],
        'baseline': {'window': {'start': (day - timedelta(days=BASELINE_DAYS)).isoformat(),
                                'end': (day - timedelta(days=1)).isoformat()},
                     **_baseline_json(baseline, readings)},
        'observed': None,
        'peers': peers,
        'common_offset': None,
        'residual': None,
        'adjusted': None,
        'threshold': None,
        'rule': {'baseline_days': BASELINE_DAYS, 'min_baseline': MIN_BASELINE, 'min_peers': MIN_PEERS,
                 'sigma': SIGMA, 'temp_floor': TEMP_FLOOR, 'rh_floor': RH_FLOOR},
    }
    if observed is not None:
        judgment['observed'] = {'sample_id': observed['id'], 'observed_day': day.isoformat(),
                                'temp_c': _round(observed['temp_c']), 'rh_pct': _round(observed['rh_pct']),
                                'source': observed['source'], 'actor': observed['actor']}

    if observed is None:
        return judgment, ShiftState.INSUFFICIENT, '缺少该班读数，待补采'
    if baseline['temp_c']['n'] < MIN_BASELINE or baseline['rh_pct']['n'] < MIN_BASELINE:
        return judgment, ShiftState.INSUFFICIENT, (
            f"前七天同班读数仅 {baseline['temp_c']['n']} 笔，待补采")
    offset = peers.get('common_offset') if peers else None
    if offset is None:
        return judgment, ShiftState.INSUFFICIENT, (
            f"全园同班可比探头仅 {peers.get('sensors', 0) if peers else 0} 个，待补采")

    raw_temp = observed['temp_c'] - baseline['temp_c']['median']
    raw_rh = observed['rh_pct'] - baseline['rh_pct']['median']
    adjusted_temp = raw_temp - offset['temp_c']
    adjusted_rh = raw_rh - offset['rh_pct']
    temp_limit = _threshold(baseline['temp_c'], TEMP_FLOOR)
    rh_limit = _threshold(baseline['rh_pct'], RH_FLOOR)
    judgment.update({
        'common_offset': {'temp_c': _round(offset['temp_c']), 'rh_pct': _round(offset['rh_pct']),
                          'sensors': offset['sensors']},
        'residual': {'temp_c': _round(raw_temp), 'rh_pct': _round(raw_rh)},
        'adjusted': {'temp_c': _round(adjusted_temp), 'rh_pct': _round(adjusted_rh)},
        'threshold': {'temp_c': _round(temp_limit), 'rh_pct': _round(rh_limit)},
    })
    if abs(adjusted_temp) >= temp_limit or abs(adjusted_rh) >= rh_limit:
        return judgment, ShiftState.ANOMALY, (
            f'扣除全园同班偏移后仍偏离基线：温 {adjusted_temp:+.1f} °C、湿 {adjusted_rh:+.1f}%')
    return judgment, ShiftState.CLEAR, '扣除全园同班偏移后无显著异常'


def _insert_evidence(db, order_id, revision, state, judgment, digest, note, actor):
    db.execute('''
        INSERT INTO inspection_evidence(order_id,revision,state,judgment,digest,note,actor)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    ''', (order_id, revision, state.value, Jsonb(judgment), digest, note, actor))


def _apply_order(db, block, day, slot, state, digest, judgment, actor, note):
    """Move the order for one sensor-shift; repeated retries keep one order row."""
    existing = db.execute('''
        SELECT * FROM inspection_order
        WHERE sensor=%s AND observed_day=%s AND slot=%s FOR UPDATE
    ''', (block['sensor'], day, slot.value)).fetchone()

    if existing is None:
        if state is not ShiftState.ANOMALY:
            return None, None
        order = db.execute('''
            INSERT INTO inspection_order(block_id,sensor,observed_day,slot,state,current_revision)
            VALUES (%s,%s,%s,%s,'open',1) RETURNING id,state
        ''', (block['id'], block['sensor'], day, slot.value)).fetchone()
        _insert_evidence(db, order['id'], 1, state, judgment, digest, note, actor)
        event(db, actor, 'inspection_opened', f"{block['code']}/{day}/{slot.value}",
              order_id=order['id'], digest=digest)
        return order['id'], order['state']

    revision = existing['current_revision'] + 1
    db.execute('''
        UPDATE inspection_order SET current_revision=%s,updated_at=now() WHERE id=%s
    ''', (revision, existing['id']))
    _insert_evidence(db, existing['id'], revision, state, judgment, digest, note, actor)

    new_state = existing['state']
    action = 'inspection_evidence_updated'
    if state == ShiftState.ANOMALY and existing['state'] == InspectionState.REVOKED:
        new_state = InspectionState.OPEN
        db.execute('''UPDATE inspection_order SET state='open',revoked_at=NULL,accepted_revision=NULL,
                      accepted_by=NULL,accepted_at=NULL,review_at=NULL,updated_at=now()
                      WHERE id=%s''', (existing['id'],))
        action = 'inspection_reopened'
    elif state == ShiftState.CLEAR and existing['state'] == InspectionState.OPEN:
        new_state = InspectionState.REVOKED
        db.execute('''UPDATE inspection_order SET state='revoked',revoked_at=now(),updated_at=now()
                      WHERE id=%s''', (existing['id'],))
        action = 'inspection_revoked_auto'
    elif state == ShiftState.CLEAR and existing['state'] == InspectionState.ACCEPTED:
        new_state = InspectionState.PENDING_REVIEW
        db.execute('''UPDATE inspection_order SET state='pending_review',review_at=now(),updated_at=now()
                      WHERE id=%s''', (existing['id'],))
        action = 'inspection_review_required'
    event(db, actor, action, f"{block['code']}/{day}/{slot.value}", order_id=existing['id'],
          revision=revision, digest=digest, state=str(new_state))
    return existing['id'], new_state


def evaluate_shift(db, day, slot, actor, note='重算该班'):
    """Judge every sensor for one date and shift, reusing or updating its order."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    slot = Slot(slot)
    start = day - timedelta(days=BASELINE_DAYS)
    blocks = db.execute('SELECT id,code,name,sensor FROM vine_block ORDER BY code').fetchall()
    observed = {row['block_id']: row for row in db.execute('''
        SELECT id,block_id,temp_c,rh_pct,source,actor
        FROM microclimate_sample WHERE observed_day=%s AND slot=%s
    ''', (day, slot.value)).fetchall()}
    prior = {}
    for row in db.execute('''
        SELECT block_id,observed_day,temp_c,rh_pct,source
        FROM microclimate_sample WHERE slot=%s AND observed_day BETWEEN %s AND %s
        ORDER BY observed_day
    ''', (slot.value, start, day - timedelta(days=1))).fetchall():
        prior.setdefault(row['block_id'], []).append(row)

    baselines = {}
    for block in blocks:
        readings = prior.get(block['id'], [])
        baselines[block['id']] = {
            'readings': readings,
            'temp_c': _stats([row['temp_c'] for row in readings]),
            'rh_pct': _stats([row['rh_pct'] for row in readings]),
        }

    eligible = [block for block in blocks
                if block['id'] in observed
                and baselines[block['id']]['temp_c']['n'] >= MIN_BASELINE
                and baselines[block['id']]['rh_pct']['n'] >= MIN_BASELINE]
    peer_residuals = []
    for block in eligible:
        baseline = baselines[block['id']]
        reading = observed[block['id']]
        peer_residuals.append({'sensor': block['sensor'], 'block_code': block['code'],
                               'temp_c': reading['temp_c'] - baseline['temp_c']['median'],
                               'rh_pct': reading['rh_pct'] - baseline['rh_pct']['median']})
    common = None
    if len(eligible) >= MIN_PEERS:
        common = {'temp_c': median(item['temp_c'] for item in peer_residuals),
                  'rh_pct': median(item['rh_pct'] for item in peer_residuals),
                  'sensors': len(eligible)}

    results = []
    for block in blocks:
        baseline = baselines[block['id']]
        peers = {'sensors': len(eligible), 'common_offset': common,
                 'readings': [{'sensor': item['sensor'], 'block_code': item['block_code'],
                               'temp_c': _round(item['temp_c']), 'rh_pct': _round(item['rh_pct'])}
                              for item in peer_residuals]}
        judgment, state, reason = _evaluate_block(
            block, day, slot, observed.get(block['id']), baseline['readings'], peers)
        judgment.update({'state': state.value, 'reason': reason})
        digest = _digest(judgment)

        shift = db.execute('''
            SELECT * FROM sensor_shift WHERE sensor=%s AND observed_day=%s AND slot=%s FOR UPDATE
        ''', (block['sensor'], day, slot.value)).fetchone()
        if shift is not None and shift['digest'] == digest and shift['state'] == state.value:
            order = db.execute('''
                SELECT id,state FROM inspection_order
                WHERE sensor=%s AND observed_day=%s AND slot=%s
            ''', (block['sensor'], day, slot.value)).fetchone()
            results.append({'block_id': block['id'], 'block_code': block['code'],
                            'block_name': block['name'], 'sensor': block['sensor'],
                            'observed_day': day, 'slot': slot.value, 'state': state.value,
                            'reason': reason, 'order_id': order['id'] if order else None,
                            'order_state': order['state'] if order else None, 'judgment': judgment})
            continue

        revision = (shift['revision'] + 1) if shift is not None else 1
        if shift is None:
            db.execute('''
                INSERT INTO sensor_shift(block_id,sensor,observed_day,slot,state,judgment,revision,digest)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ''', (block['id'], block['sensor'], day, slot.value, state.value,
                  Jsonb(judgment), revision, digest))
        else:
            db.execute('''
                UPDATE sensor_shift SET block_id=%s,state=%s,judgment=%s,revision=%s,digest=%s,
                       updated_at=now() WHERE id=%s
            ''', (block['id'], state.value, Jsonb(judgment), revision, digest, shift['id']))

        order_id, order_state = _apply_order(db, block, day, slot, state, digest, judgment, actor, note)
        results.append({'block_id': block['id'], 'block_code': block['code'],
                        'block_name': block['name'], 'sensor': block['sensor'],
                        'observed_day': day, 'slot': slot.value, 'state': state.value,
                        'reason': reason, 'order_id': order_id, 'order_state': order_state,
                        'judgment': judgment})
    return results


def recompute_after_sample(db, day, slot, actor):
    """Late reading: rejudge its own shift and every already-judged shift that uses the day as baseline."""
    slot = Slot(slot)
    day = date.fromisoformat(day) if isinstance(day, str) else day
    later = db.execute('''
        SELECT DISTINCT observed_day FROM sensor_shift
        WHERE slot=%s AND observed_day BETWEEN %s AND %s
    ''', (slot.value, day, day + timedelta(days=BASELINE_DAYS))).fetchall()
    for target in sorted({day} | {row['observed_day'] for row in later}):
        evaluate_shift(db, target, slot, actor, '晚到读数自动重算该班')


def shifts(db, state=None, observed_day=None):
    filters, params = [], []
    if state is not None:
        try:
            state = ShiftState(state)
        except ValueError:
            raise HTTPException(422, '未知的班次判断状态')
        filters.append('s.state=%s')
        params.append(state.value)
    if observed_day is not None:
        filters.append('s.observed_day=%s')
        params.append(observed_day)
    where = ('WHERE ' + ' AND '.join(filters)) if filters else ''
    return db.execute(f'''
        SELECT s.*,b.code AS block_code,b.name AS block_name,
               o.id AS order_id,o.state AS order_state
        FROM sensor_shift s JOIN vine_block b ON b.id=s.block_id
        LEFT JOIN inspection_order o ON o.sensor=s.sensor AND o.observed_day=s.observed_day
             AND o.slot=s.slot
        {where}
        ORDER BY s.observed_day DESC,s.slot,b.code LIMIT 300
    ''', params).fetchall()


def _order_row(db, order_id):
    return _one(db, '''
        SELECT o.*,b.code AS block_code,b.name AS block_name,
               s.state AS shift_state,s.judgment AS current_judgment,s.revision AS shift_revision
        FROM inspection_order o JOIN vine_block b ON b.id=o.block_id
        LEFT JOIN sensor_shift s ON s.sensor=o.sensor AND s.observed_day=o.observed_day
             AND s.slot=o.slot
        WHERE o.id=%s
    ''', (order_id,), '巡检单')


def _with_evidence(db, rows):
    if not rows:
        return rows
    evidence = db.execute('''
        SELECT order_id,revision,state,judgment,digest,note,actor,created_at
        FROM inspection_evidence WHERE order_id=ANY(%s) ORDER BY order_id,revision
    ''', ([row['id'] for row in rows],)).fetchall()
    grouped = {}
    for item in evidence:
        grouped.setdefault(item.pop('order_id'), []).append(item)
    for row in rows:
        row['evidence'] = grouped.get(row['id'], [])
    return rows


def orders(db, state=None):
    params = []
    where = ''
    if state is not None:
        try:
            state = InspectionState(state)
        except ValueError:
            raise HTTPException(422, '未知的巡检单状态')
        where = 'WHERE o.state=%s'
        params.append(state.value)
    rows = db.execute(f'''
        SELECT o.*,b.code AS block_code,b.name AS block_name,
               s.state AS shift_state,s.judgment AS current_judgment,s.revision AS shift_revision
        FROM inspection_order o JOIN vine_block b ON b.id=o.block_id
        LEFT JOIN sensor_shift s ON s.sensor=o.sensor AND s.observed_day=o.observed_day
             AND s.slot=o.slot
        {where}
        ORDER BY CASE o.state WHEN 'pending_review' THEN 0 WHEN 'open' THEN 1
                 WHEN 'accepted' THEN 2 ELSE 3 END,o.observed_day DESC,o.slot,b.code
        LIMIT 200
    ''', params).fetchall()
    return _with_evidence(db, rows)


def order(db, order_id):
    return _with_evidence(db, [_order_row(db, order_id)])[0]


def accept(db, order_id, actor):
    row = db.execute('SELECT * FROM inspection_order WHERE id=%s FOR UPDATE', (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '巡检单不存在')
    if row['state'] != InspectionState.OPEN:
        raise HTTPException(409, ORDER_MESSAGES.get(InspectionState(row['state']), '该巡检单不能接单'))
    db.execute('''
        UPDATE inspection_order SET state='accepted',accepted_revision=current_revision,
               accepted_by=%s,accepted_at=now(),updated_at=now() WHERE id=%s
    ''', (actor, order_id))
    event(db, actor, 'inspection_accepted', f"{row['sensor']}/{row['observed_day']}/{row['slot']}",
          order_id=order_id, revision=row['current_revision'])
    return order(db, order_id)


def review(db, order_id, outcome, reason, actor):
    row = db.execute('SELECT * FROM inspection_order WHERE id=%s FOR UPDATE', (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '巡检单不存在')
    if row['state'] != InspectionState.PENDING_REVIEW:
        raise HTTPException(409, '只有待复核的巡检单可以复核')
    if outcome == 'confirm':
        db.execute('''UPDATE inspection_order SET state='accepted',review_at=NULL,updated_at=now()
                      WHERE id=%s''', (order_id,))
        action = 'inspection_review_confirmed'
    elif outcome == 'revoke':
        db.execute('''UPDATE inspection_order SET state='revoked',review_at=NULL,revoked_at=now(),
                      updated_at=now() WHERE id=%s''', (order_id,))
        action = 'inspection_review_revoked'
    else:
        raise HTTPException(422, '复核结论只能是 confirm 或 revoke')
    event(db, actor, action, f"{row['sensor']}/{row['observed_day']}/{row['slot']}",
          order_id=order_id, reason=reason, accepted_revision=row['accepted_revision'])
    return order(db, order_id)
