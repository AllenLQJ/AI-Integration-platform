"""Bounded, sanitized activity contract for timeline observation and external reporting.

External callers never choose trusted provenance. Paths are workspace-relative when
possible; full file contents, env maps and credentials are never retained.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from .models import HubError, redact

ACTIVITY_KINDS = frozenset({'message', 'file_read', 'command', 'test', 'file_change', 'system'})
ACTIVITY_MAX_JSON_BYTES = 65536
OUTPUT_EXCERPT_BUDGET = 16384
SCALAR_LIMITS = {
    'tool_name': 200,
    'command_display': 4000,
    'cwd_display': 500,
    'path': 1000,
    'path_display': 500,
    'title': 500,
    'preview': 500,
    'text': 100000,
    'operation_id': 200,
    'phase': 64,
    'status': 64,
    'actor': 200,
    'recipient': 200,
    'source': 200,
    'test_summary': 2000,
    'change_summary': 2000,
}
# Shared UTF-8 budget covers all retained operation strings/metadata (not only stdout/stderr).
OPERATION_FIELD_PRIORITY = (
    'path', 'path_display', 'path_scope', 'tool_name', 'exit_code', 'duration_ms', 'read_range',
    'cwd_display', 'command_display', 'test_summary', 'change_summary',
    'stdout_excerpt', 'stderr_excerpt',
)
ALLOWED_OPERATION = frozenset({
    'tool_name', 'path', 'path_display', 'path_scope', 'read_range', 'command_display',
    'cwd_display', 'exit_code', 'duration_ms', 'stdout_excerpt', 'stderr_excerpt',
    'test_summary', 'change_summary', 'truncated_fields',
})
ALLOWED_TOP = frozenset({
    'kind', 'timestamp', 'source', 'completeness', 'actor', 'recipient', 'title', 'preview',
    'text', 'operation_id', 'phase', 'status', 'operation', 'provider_message_id',
    'provider_revision', 'upstream_timestamp',
})
STATUS_VALUES = frozenset({'started', 'completed', 'failed', 'unknown'})
PHASE_VALUES = frozenset({'started', 'completed', 'failed', 'progress', 'unknown'})
PATH_SCOPE = frozenset({'workspace', 'outside', 'unknown'})


def _utf8_len(value: str) -> int:
    return len(value.encode('utf-8'))


def _bound_str(value, name, maximum, required=False):
    if value is None:
        if required:
            raise HubError('INVALID_ARGUMENT', name + ' 需要非空字符串')
        return None, False
    if not isinstance(value, str) or '\x00' in value:
        raise HubError('INVALID_ARGUMENT', name + ' 需要不含空字符的字符串')
    value = redact(value)
    if _utf8_len(value) > maximum:
        while value and _utf8_len(value) > maximum:
            value = value[:-1]
        return value, True
    if required and not value:
        raise HubError('INVALID_ARGUMENT', name + ' 需要非空字符串')
    return value, False


def relative_path_display(path, workspace=''):
    """Return safe relative metadata; never expand outside the workspace."""
    if not isinstance(path, str) or not path or '\x00' in path:
        return dict(path_display='[PATH]', path_scope='unknown', path='')
    raw = path.replace('\r', '').replace('\n', '')
    if not workspace:
        redacted = redact(raw)
        if re.search(r'(?i)(?:[a-z]:[\\/]|\\\\|/)', redacted):
            name = PureWindowsPath(redacted).name or PurePosixPath(redacted).name or '[PATH]'
            return dict(path_display=name[:SCALAR_LIMITS['path_display']], path_scope='outside', path='')
        return dict(path_display=redacted[:SCALAR_LIMITS['path_display']], path_scope='unknown', path='')
    try:
        root = Path(workspace).resolve()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        relative = candidate.relative_to(root)
        display = relative.as_posix()
        if '..' in PurePosixPath(display).parts:
            raise ValueError('escape')
        return dict(path=display[:SCALAR_LIMITS['path']],
                    path_display=display[:SCALAR_LIMITS['path_display']],
                    path_scope='workspace')
    except (OSError, ValueError, RuntimeError):
        name = PureWindowsPath(raw).name or PurePosixPath(raw).name or '[PATH]'
        return dict(path='', path_display=name[:SCALAR_LIMITS['path_display']], path_scope='outside')


def _excerpt(value, budget_left, truncated_fields, field):
    if value is None:
        return None, budget_left
    if not isinstance(value, str):
        raise HubError('INVALID_ARGUMENT', field + ' 需要字符串')
    value = redact(value)
    encoded = value.encode('utf-8')
    if len(encoded) <= budget_left:
        return value, budget_left - len(encoded)
    cut = encoded[:budget_left].decode('utf-8', errors='ignore')
    truncated_fields.append(field)
    return cut, 0


def _apply_operation_budget(safe_op, truncated_fields, budget=OUTPUT_EXCERPT_BUDGET):
    """Charge every retained operation string/metadata against one UTF-8 budget."""
    remaining = budget
    kept = {}
    for key in OPERATION_FIELD_PRIORITY:
        if key not in safe_op:
            continue
        value = safe_op[key]
        if isinstance(value, str):
            text, remaining = _excerpt(value, remaining, truncated_fields, 'operation.' + key)
            if text is not None:
                kept[key] = text
        elif isinstance(value, dict):
            encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(encoded) <= remaining:
                kept[key] = value
                remaining -= len(encoded)
            else:
                truncated_fields.append('operation.' + key)
        else:
            encoded = str(value).encode('utf-8')
            if len(encoded) <= remaining:
                kept[key] = value
                remaining -= len(encoded)
            else:
                truncated_fields.append('operation.' + key)
    return kept


def normalize_activity(activity, workspace='', *, force_provenance=None):
    """Validate and sanitize a whitelisted activity dict.

    force_provenance overrides any caller-supplied provenance (external writes must
    pass 'controller_reported'). Executor-observed activities omit caller provenance.
    """
    if not isinstance(activity, dict):
        raise HubError('INVALID_ARGUMENT', 'activity 需要对象')
    try:
        raw = json.dumps(activity, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError) as error:
        raise HubError('INVALID_ARGUMENT', 'activity 必须为严格 JSON（禁止 NaN/Infinity）') from error
    if len(raw) > ACTIVITY_MAX_JSON_BYTES:
        raise HubError('INVALID_ARGUMENT', 'activity JSON 超过 65536 字节')
    unknown = set(activity) - ALLOWED_TOP - {'provenance'}
    if unknown:
        raise HubError('INVALID_ARGUMENT', 'activity 含未知字段: ' + ', '.join(sorted(unknown)[:8]))
    kind = activity.get('kind')
    if kind not in ACTIVITY_KINDS:
        raise HubError('INVALID_ARGUMENT', 'activity.kind 不在允许列表中')
    truncated = []
    result = {'kind': kind}
    for key in ('source', 'actor', 'recipient', 'operation_id', 'provider_message_id'):
        if key in activity:
            value, was_cut = _bound_str(activity[key], key, SCALAR_LIMITS.get(key, 200))
            if value is not None:
                result[key] = value
            if was_cut:
                truncated.append(key)
    for key in ('title', 'preview', 'text'):
        if key in activity:
            value, was_cut = _bound_str(activity[key], key, SCALAR_LIMITS[key])
            if value is not None:
                result[key] = value
            if was_cut:
                truncated.append(key)
    if 'completeness' in activity:
        completeness = activity['completeness']
        if completeness not in {'complete', 'recorded', 'truncated', 'possibly_truncated', 'unavailable'}:
            raise HubError('INVALID_ARGUMENT', 'completeness 无效')
        result['completeness'] = completeness
    if 'status' in activity:
        if activity['status'] not in STATUS_VALUES:
            raise HubError('INVALID_ARGUMENT', 'status 无效')
        result['status'] = activity['status']
    if 'phase' in activity:
        if activity['phase'] not in PHASE_VALUES:
            raise HubError('INVALID_ARGUMENT', 'phase 无效')
        result['phase'] = activity['phase']
    if 'provider_revision' in activity:
        revision = activity['provider_revision']
        if type(revision) is not int or not 0 <= revision <= 9223372036854775807:
            raise HubError('INVALID_ARGUMENT', 'provider_revision 需要非负整数')
        result['provider_revision'] = revision
    if 'timestamp' in activity:
        stamp = activity['timestamp']
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise HubError('INVALID_ARGUMENT', 'timestamp 无效')
        if not 0 <= float(stamp) <= 4102444800:
            raise HubError('INVALID_ARGUMENT', 'timestamp 超出合理范围')
        result['timestamp'] = float(stamp)
    if 'upstream_timestamp' in activity:
        stamp = activity['upstream_timestamp']
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise HubError('INVALID_ARGUMENT', 'upstream_timestamp 无效')
        if not 0 <= float(stamp) <= 4102444800:
            raise HubError('INVALID_ARGUMENT', 'upstream_timestamp 超出合理范围')
        result['upstream_timestamp'] = float(stamp)
    operation = activity.get('operation')
    if operation is not None:
        if not isinstance(operation, dict):
            raise HubError('INVALID_ARGUMENT', 'operation 需要对象')
        unknown_op = set(operation) - ALLOWED_OPERATION
        if unknown_op:
            raise HubError('INVALID_ARGUMENT', 'operation 含未知字段: ' + ', '.join(sorted(unknown_op)[:8]))
        safe_op = {}
        if 'path' in operation or 'path_display' in operation:
            meta = relative_path_display(operation.get('path') or operation.get('path_display') or '', workspace)
            safe_op.update(meta)
        for key in ('tool_name', 'command_display', 'cwd_display', 'test_summary', 'change_summary'):
            if key in operation:
                value, was_cut = _bound_str(operation[key], key, SCALAR_LIMITS.get(key, 2000))
                if value is not None:
                    safe_op[key] = value
                if was_cut:
                    truncated.append('operation.' + key)
        if 'read_range' in operation:
            rng = operation['read_range']
            if not isinstance(rng, dict):
                raise HubError('INVALID_ARGUMENT', 'read_range 需要对象')
            start, end = rng.get('start'), rng.get('end')
            if any(v is not None and (type(v) is not int or v < 0) for v in (start, end)):
                raise HubError('INVALID_ARGUMENT', 'read_range 无效')
            safe_op['read_range'] = {k: rng[k] for k in ('start', 'end') if k in rng and type(rng[k]) is int}
        if 'exit_code' in operation:
            code = operation['exit_code']
            if type(code) is not int or not -2147483648 <= code <= 2147483647:
                raise HubError('INVALID_ARGUMENT', 'exit_code 无效')
            safe_op['exit_code'] = code
        if 'duration_ms' in operation:
            duration = operation['duration_ms']
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                raise HubError('INVALID_ARGUMENT', 'duration_ms 无效')
            if duration > 2**53:
                raise HubError('INVALID_ARGUMENT', 'duration_ms 超出合理范围')
            safe_op['duration_ms'] = int(duration)
        for field in ('stdout_excerpt', 'stderr_excerpt'):
            if field in operation:
                if not isinstance(operation[field], str):
                    raise HubError('INVALID_ARGUMENT', 'operation.' + field + ' 需要字符串')
                safe_op[field] = redact(operation[field])
        if 'truncated_fields' in operation:
            fields = operation['truncated_fields']
            if not isinstance(fields, list) or any(not isinstance(item, str) for item in fields):
                raise HubError('INVALID_ARGUMENT', 'truncated_fields 需要字符串列表')
            truncated.extend(fields[:20])
        if safe_op:
            result['operation'] = _apply_operation_budget(safe_op, truncated)
    if force_provenance is not None:
        if force_provenance not in {'executor_observed', 'controller_reported', 'legacy_summary'}:
            raise HubError('INVALID_ARGUMENT', 'provenance 无效')
        result['provenance'] = force_provenance
    elif 'provenance' in activity:
        # Callers cannot self-attest trusted observation through normalize alone.
        raise HubError('INVALID_ARGUMENT', '不允许调用方自行设置 provenance')
    if truncated:
        result['truncated_fields'] = list(dict.fromkeys(truncated))[:40]
        if result.get('completeness') not in {'truncated', 'unavailable'}:
            result['completeness'] = 'truncated'
    elif 'completeness' not in result:
        result['completeness'] = 'recorded'
    # Re-check serialized size after normalization.
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError) as error:
        raise HubError('INVALID_ARGUMENT', '规范化后的 activity 不是严格 JSON') from error
    if len(encoded) > ACTIVITY_MAX_JSON_BYTES:
        raise HubError('INVALID_ARGUMENT', '规范化后的 activity 超过 65536 字节')
    return result


def activity_digest(event_id, activity):
    payload = json.dumps({'event_id': event_id, 'activity': activity}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
