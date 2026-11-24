from datetime import date, timedelta

from backend.src.auth import hash_password
from backend.src.clusters import submit
from backend.src.db import connect, lock_ledger
from backend.src.domain import Slot
from backend.src.inspection import accept, evaluate_shift

BASE_END = date(2026, 11, 14)
NEXT_END = date(2026, 11, 20)
SHIFT_DAY = date(2026, 11, 21)
PASSWORD = 'Vineyard271!'
BLOCKS = (
    ('V-01', '河湾低畦', '霞多丽', 1.22, 'WX-301', 90, 430, 195, 144),
    ('V-02', '北坡旧藤', '霞多丽', 1.56, 'WX-302', 317, 610, 243, 177),
    ('V-03', '石墙中段', '马瑟兰', 1.84, 'WX-303', 366, 369, 224, 174),
    ('V-04', '风口平台', '马瑟兰', 1.32, 'WX-304', 649, 590, 205, 162),
    ('V-05', '南向砾石台', '赤霞珠', 1.90, 'WX-305', 658, 319, 258, 175),
    ('V-06', '东侧新垄', '赤霞珠', 1.45, 'WX-306', 383, 108, 236, 140),
)
BASE = ((22.0, 11.5, 72, 89), (22.2, 11.7, 71, 88), (25.1, 14.0, 57, 76),
        (25.0, 14.2, 58, 76), (31.0, 20.0, 35, 55), (31.1, 20.2, 36, 54))
WARM = (31.2, 20.0, 35, 55)
WINDOWS = ((BASE_END, 2), (NEXT_END, 2))
HISTORY_START = date(2026, 11, 15)  # filler days between the two cluster windows


def climate(index, day, slot):
    """Baseline climate per block; V-02 turns warm once the north slope changes."""
    if index == 1 and day > BASE_END:
        return WARM
    return BASE[index]


def insert_sample(db, block, day, phase, temp_c, rh_pct, source):
    db.execute('''
        INSERT INTO microclimate_sample(block_id,sensor,observed_day,slot,temp_c,rh_pct,source,actor)
        SELECT %s,%s,%s,%s,%s,%s,%s,'operator' WHERE NOT EXISTS (
            SELECT 1 FROM microclimate_sample WHERE block_id=%s AND observed_day=%s AND slot=%s
        )
    ''', (block['id'], block['sensor'], day, phase, temp_c, rh_pct, source,
          block['id'], day, phase))


def seed_samples(db, row, index):
    # The two documented three-day cluster windows keep their published readings.
    for end, _ in WINDOWS:
        for offset in (2, 1, 0):
            day = end - timedelta(days=offset)
            values = climate(index, day, 'day')
            for j, phase in enumerate(('day', 'night')):
                if index == 5 and end == NEXT_END and phase == 'night' and offset in (0, 1):
                    continue
                insert_sample(db, row, day, phase, values[j] + (1 - offset) * 0.2,
                              values[j + 2] + offset - 1, f'wx-{end:%Y%m%d}.csv')
    # Filler history lets every shift carry a seven-day same-shift baseline.
    day = HISTORY_START
    while day < NEXT_END:
        values = climate(index, day, 'day')
        drift = (day - HISTORY_START).days % 3 - 1
        for j, phase in enumerate(('day', 'night')):
            insert_sample(db, row, day, phase, values[j] + drift * 0.1,
                          values[j + 2] + drift * 0.5, f'wx-{day:%Y%m%d}.csv')
        day += timedelta(days=1)
    # Shift under inspection: the whole orchard cools, two blocks drop further,
    # and the east block's day reading arrives late.
    values = climate(index, SHIFT_DAY, 'day')
    if index != 5:
        drop = 3.5 if index in (2, 3) else 1.5
        insert_sample(db, row, SHIFT_DAY, 'day', values[0] - drop, values[2],
                      f'wx-{SHIFT_DAY:%Y%m%d}.csv')
        insert_sample(db, row, SHIFT_DAY, 'night', values[1] - 1.5, values[3],
                      f'wx-{SHIFT_DAY:%Y%m%d}.csv')


def seed_inspection(db):
    if db.execute('SELECT 1 FROM sensor_shift LIMIT 1').fetchone():
        return
    for day in (NEXT_END, SHIFT_DAY):
        for slot in Slot:
            evaluate_shift(db, day, slot, 'operator', '演示初始化')
    target = db.execute('''
        SELECT o.id FROM inspection_order o JOIN vine_block b ON b.id=o.block_id
        WHERE b.code='V-04' AND o.observed_day=%s AND o.slot='day'
    ''', (SHIFT_DAY,)).fetchone()
    if target is None:
        return
    accept(db, target['id'], 'operator')
    row = db.execute("SELECT id,sensor FROM vine_block WHERE code='V-06'").fetchone()
    values = climate(5, SHIFT_DAY, 'day')
    insert_sample(db, row, SHIFT_DAY, 'day', values[0] - 9, values[2],
                  f'wx-late-{SHIFT_DAY:%Y%m%d}.csv')
    evaluate_shift(db, SHIFT_DAY, Slot.DAY, 'operator', '晚到读数自动重算该班')


def seed():
    with connect() as db:
        lock_ledger(db)
        for name in ('operator', 'supervisor', 'viewer'):
            if not db.execute('SELECT 1 FROM account WHERE username=%s', (name,)).fetchone():
                db.execute('INSERT INTO account VALUES (%s,%s,%s)', (name, name, hash_password(PASSWORD)))
        for index, block in enumerate(BLOCKS):
            db.execute('''
                INSERT INTO vine_block(code,name,cultivar,area_ha,sensor,east_m,north_m,width_m,height_m)
                SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s WHERE NOT EXISTS (SELECT 1 FROM vine_block WHERE code=%s)
            ''', (*block, block[0]))
            row = db.execute('SELECT id,sensor FROM vine_block WHERE code=%s', (block[0],)).fetchone()
            seed_samples(db, row, index)
        if not db.execute('SELECT 1 FROM climate_run LIMIT 1').fetchone():
            submit(db, BASE_END, 'operator')
        seed_inspection(db)


if __name__ == '__main__':
    seed()
    print('Seed ready: six blocks, field windows, inspection shifts, and three accounts.')
