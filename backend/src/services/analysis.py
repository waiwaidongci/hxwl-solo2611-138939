from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from itertools import groupby
from statistics import median


PARAMETERS = {
    'window_batches': ('历史窗口批数', 5, 3, 60),
    'min_history': ('最少历史批数', 4, 3, 60),
    'lookback_days': ('回溯时限', 14, 1, 90),
    'mad_multiplier': ('稳健离差倍数', Decimal('3.5'), 1, 20),
    'zero_mad_tolerance': ('零 MAD 绝对容差', Decimal('0.15'), Decimal('0.01'), 10),
}
MAD_SCALE = Fraction('1.4826')
DAY_START = time(8)
NIGHT_START = time(20)
HIGH, LOW, NORMAL, PENDING = '偏高', '偏低', '正常', '历史不足'
ACTIONS = {
    HIGH: '复查蒸汽阀、疏水器和保温时长',
    LOW: '核对蒸汽表起止读数、旁路和干布净重',
}


@dataclass(frozen=True)
class Batch:
    batch_id: str
    ended_at: datetime
    group: tuple[str, str, str]
    fabric: float
    steam: float
    rate: Fraction


def _number(value, label, lower, upper):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label}必须是数字') from None
    if not number.is_finite() or not lower <= number <= upper:
        raise ValueError(f'{label}须在 {lower} 至 {upper} 之间')
    return number


def _text(row, key, label):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}不能为空')
    return value.strip()


def _ended_at(row):
    day_text = _text(row, 'end_date', '完工日期')
    clock_text = _text(row, 'end_time', '完工时刻')
    try:
        day = date.fromisoformat(day_text)
        clock = time.fromisoformat(clock_text)
        if day.isoformat() != day_text or len(clock_text) != 5 or clock.isoformat(timespec='minutes') != clock_text:
            raise ValueError
    except ValueError:
        raise ValueError('完工日期须为 YYYY-MM-DD，完工时刻须为北京时间 HH:MM（24 小时制）') from None
    return datetime.combine(day, clock)


def _load_batches(rows):
    batches = []
    seen_ids = set()
    for index, row in enumerate(rows, start=1):
        try:
            batch_id = _text(row, 'batch_id', '染色批号')
            if batch_id in seen_ids:
                raise ValueError(f'染色批号 {batch_id} 重复')
            machine = _text(row, 'machine', '染机编号')
            recipe = _text(row, 'recipe', '工艺配方')
            ended_at = _ended_at(row)
            shift = '昼班' if DAY_START <= ended_at.time() < NIGHT_START else '夜班'
            fabric = float(_number(row.get('fabric_kg'), '干布净重（kg）', Decimal('0.01'), 10_000_000))
            steam = float(_number(row.get('steam_kg'), '蒸汽用量（kg）', 0, 10_000_000))
        except ValueError as error:
            raise ValueError(f'数据第 {index} 行 · {error}') from None
        # Preserve exact ratios when the imported masses yield repeating decimals.
        rate = Fraction(str(steam)) / Fraction(str(fabric))
        batches.append(Batch(batch_id, ended_at, (machine, recipe, shift), fabric, steam, rate))
        seen_ids.add(batch_id)
    if not batches:
        raise ValueError('至少需要一条染色批次记录')
    return batches


def validate_rows(rows):
    _load_batches(rows)


def _parameters(params):
    if params.keys() - PARAMETERS.keys():
        raise ValueError('存在未知排查参数，请核对历史窗口和阈值设置')
    settings = {}
    for key, (label, default, lower, upper) in PARAMETERS.items():
        value = _number(params.get(key, default), label, lower, upper)
        if isinstance(default, int):
            if value != value.to_integral_value():
                raise ValueError(f'{label}必须是整数')
            settings[key] = int(value)
        else:
            settings[key] = Fraction(value)
    if settings['min_history'] > settings['window_batches']:
        raise ValueError('最少历史批数不能超过历史窗口批数')
    return settings


def _display(value):
    return float(value) if value is not None else None


def analyze(rows, params):
    batches = sorted(_load_batches(rows), key=lambda batch: (batch.ended_at, batch.batch_id))
    settings = _parameters(params)
    min_history = settings['min_history']
    max_age = timedelta(days=settings['lookback_days'])
    history = defaultdict(lambda: deque(maxlen=settings['window_batches']))
    trace = []
    flagged = []
    counts = Counter()
    group_flags = Counter()

    for ended_at, simultaneous in groupby(batches, key=lambda batch: batch.ended_at):
        current = list(simultaneous)
        for batch in current:
            prior = history[batch.group]
            while prior and ended_at - prior[0].ended_at > max_age:
                prior.popleft()
            baseline = mad = delta = limit = None
            if len(prior) < min_history:
                status, rule = PENDING, f'需要至少 {min_history} 个历史批次'
            else:
                baseline = median(item.rate for item in prior)
                mad = median(abs(item.rate - baseline) for item in prior)
                delta = batch.rate - baseline
                if mad == 0:
                    limit, rule = settings['zero_mad_tolerance'], '零 MAD · 绝对容差'
                else:
                    limit = settings['mad_multiplier'] * MAD_SCALE * mad
                    rule = '滚动 MAD · 稳健阈值'
                if abs(delta) > limit:
                    status = HIGH if delta > 0 else LOW
                    flagged.append((batch, baseline, delta, limit, status))
                    group_flags[batch.group] += 1
                else:
                    status = NORMAL
            counts[status] += 1
            trace.append([
                batch.batch_id, ended_at.isoformat(sep=' ', timespec='minutes'), *batch.group,
                status, len(prior), batch.fabric, batch.steam, _display(batch.rate),
                _display(baseline), _display(mad), _display(delta), _display(limit), rule,
                ' / '.join(item.batch_id for item in prior),
            ])
        # Equal timestamps cannot influence each other, even across CSV rows from one group.
        for batch in current:
            history[batch.group].append(batch)

    flagged.sort(key=lambda item: (item[4] != HIGH, -abs(item[2]), item[0].ended_at, item[0].batch_id))
    queue = [
        [batch.batch_id, batch.ended_at.isoformat(sep=' ', timespec='minutes'), *batch.group,
         status, _display(batch.rate), _display(baseline), _display(delta), _display(limit), ACTIONS[status]]
        for batch, baseline, delta, limit, status in flagged
    ]
    assessed = len(batches) - counts[PENDING]
    summary = (f'{len(batches)} 批中判定 {assessed} 批，偏高 {counts[HIGH]} 批、偏低 {counts[LOW]} 批，'
               f'历史不足 {counts[PENDING]} 批。')
    if flagged:
        batch, _, delta, _, status = flagged[0]
        summary += f'先排查 {batch.batch_id}（{status} {float(abs(delta)):.3g} kg/kg），{ACTIONS[status]}。'
    elif assessed:
        summary += '已判定批次均在阈值内，按班交接。'
    else:
        summary += '请补齐相同工况的早期批次后重算。'

    groups = sorted(group_flags.items(), key=lambda item: (-item[1], item[0]))[:12]
    return {
        'summary': summary,
        'ready': assessed > 0,
        'metrics': [
            {'label': '完成判定', 'value': assessed, 'detail': f'{counts[NORMAL]} 批在阈值内，共录入 {len(batches)} 批'},
            {'label': '偏高待查', 'value': counts[HIGH], 'detail': '工艺值班组核查用汽工序'},
            {'label': '偏低待查', 'value': counts[LOW], 'detail': '能源计量组核对批次抄表'},
            {'label': '历史不足', 'value': counts[PENDING], 'detail': f'每批至少需要 {min_history} 个同组历史批'},
        ],
        'tables': [
            {
                'title': '超差批次排查顺序',
                'columns': ['染色批号', '完工时间', '染机', '配方', '班别', '判定', '单耗（kg/kg）',
                            '基线（kg/kg）', '有向偏差（kg/kg）', '阈值（kg/kg）', '复核动作'],
                'rows': queue,
            },
            {
                'title': '逐批判定追溯',
                'columns': ['染色批号', '完工时间', '染机', '配方', '班别', '判定', '历史批数',
                            '净重（kg）', '蒸汽（kg）', '单耗（kg/kg）', '基线（kg/kg）', 'MAD（kg/kg）',
                            '有向偏差（kg/kg）', '阈值（kg/kg）', '判定规则', '入窗历史批号（由早到晚）'],
                'rows': trace,
            },
        ],
        'notes': [
            '同染机、配方、完工班别分别估计基线。采用回溯期内严格早于本批完工的最近批次，历史批次保留原实测值。',
            '昼班 08:00–19:59，夜班 20:00–次日 07:59。若窗口上限截断同一历史分钟的批次，按批号排序取最近端。',
            '绝对偏差严格超阈才进排查单。偏高先审，同方向按偏差幅度降序。自动分发需要至少完成一批判定。',
        ],
        'series_title': '超差批数最多的工况',
        'series': [{'label': ' / '.join(group), 'value': count} for group, count in groups],
    }
