"""仅依据系统电源事件核对期限，不用脚本恢复采样时间推断唤醒。"""
from datetime import datetime
import re


def power_category(row):
    # pmset 的类别栏与消息以制表符分开；Wake Requests 是独立类别。
    fields = row[25:].lstrip(' ')
    if '\t' in fields:
        return fields.split('\t', 1)[0].strip()
    match = re.match(r'^(Sleep|DarkWake|Wake)(?: {2,}|$)', fields)
    return match[1] if match else None


def full_wake_rows(rows):
    # 事件类别是 Wake 即为完整唤醒；消息可包含 DarkWake to FullWake。
    return [row for row in rows if power_category(row) == 'Wake']


def evaluate_cycle(sleep_rows, wake_rows, deadline):
    def stamp(row):
        return datetime.strptime(row[:25], '%Y-%m-%d %H:%M:%S %z').timestamp()

    sleep_time = stamp(sleep_rows[0]) if sleep_rows else None
    wake_time = stamp(wake_rows[0]) if wake_rows else None
    # pmset 时间只有秒精度，保守采用休眠开始的上界与唤醒的下界。
    crossed = bool(sleep_time is not None and wake_time is not None
                   and sleep_time + 1 <= deadline < wake_time)
    return dict(sleep_event_time=sleep_time, wake_event_time=wake_time,
                actual_sleep_seconds=wake_time - sleep_time
                if sleep_time is not None and wake_time is not None else None,
                deadline_crossed=crossed, timestamp_resolution_seconds=1)


def evaluate_power_log(rows, deadline):
    cycles = []
    sleeping = None
    for row in rows:
        category = power_category(row)
        if category not in {'Sleep', 'DarkWake', 'Wake'}:
            continue
        if category == 'Sleep' and 'Entering Sleep state' in row:
            sleeping = row
        elif category in {'DarkWake', 'Wake'} and sleeping:
            cycles.append(dict(evaluate_cycle([sleeping], [row], deadline),
                               sleep_row=sleeping, resume_row=row,
                               resume_kind=category))
            sleeping = None
    selected = next((cycle for cycle in cycles if cycle['deadline_crossed']),
                    cycles[0] if cycles else evaluate_cycle([], [], deadline))
    return dict(selected, cycles=cycles)


def timeout_processed_after_resume(cycle, timed_out_at):
    wake = cycle.get('wake_event_time')
    # Sleep 日志可能在入睡通知阶段生成，还需 Hub 的终态时间证明恢复后处理。
    return bool(wake is not None and timed_out_at is not None
                and timed_out_at >= wake + cycle.get('timestamp_resolution_seconds', 1))
