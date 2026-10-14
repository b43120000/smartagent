"""Explicit typed bridge values; never repair arbitrary malformed JSON."""
import json
import re

PATH_FIELDS = frozenset({'path', 'project_root', 'output_path'})
PATH_LIST_FIELDS = frozenset({'paths'})
STABLE_FIELDS = PATH_FIELDS | PATH_LIST_FIELDS | {'expected_filename'}
MAX_FIELD_CHARS = 16000


def _json_value(text):
    raw = text.strip()
    wrapper = re.fullmatch(r'\[V9_VALUE\]\s*\n([\s\S]*?)\n\[/V9_VALUE\]', raw)
    fence = re.fullmatch(r'```(?:json)?\s*\n([\s\S]*?)\n```', raw, re.I)
    if wrapper or fence:
        raw = (wrapper or fence).group(1).strip()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite_json_value')))
        return value, None
    except json.JSONDecodeError as exc:
        return None, {'reason':'invalid_json_value', 'detail':exc.msg,
                      'line':exc.lineno, 'column':exc.colno, 'offset':exc.pos,
                      'hint':'JSON strings require escaped backslashes (\\\\). For paths, use V9_PATH / V9_PATHS literal tags instead.'}
    except ValueError as exc:
        return None, {'reason':str(exc), 'detail':'Only one strict JSON value is accepted.'}


def stable_value_valid(field, value):
    if not isinstance(field,str):
        return False
    if field in PATH_LIST_FIELDS:
        return isinstance(value,list) and 1 <= len(value) <= 32 and all(
            isinstance(item,str) and bool(item.strip()) and not any(ord(c)<32 for c in item) for item in value)
    return field in STABLE_FIELDS and isinstance(value,str) and bool(value.strip()) and not any(ord(c)<32 for c in value)


def parse_stable_field(text, field):
    raw = str(text or '').replace('\r\n','\n')
    if len(raw) > MAX_FIELD_CHARS:
        return None, {'reason':'field_value_too_large', 'detail':field}
    if field in PATH_LIST_FIELDS:
        tagged = re.fullmatch(r'\[V9_PATHS\]\n([\s\S]*?)\n\[/V9_PATHS\]', raw)
        if tagged:
            # Literal lines: no JSON unescaping, quote stripping, ADS removal,
            # drive guessing or resolution. Security is checked after expansion.
            value = tagged.group(1).split('\n')
            error = None
        else:
            value, error = _json_value(raw)
    else:
        tagged = re.fullmatch(r'\[(V9_PATH|V9_VALUE)\]\n([\s\S]*?)\n\[/\1\]', raw)
        if tagged:
            value,error = tagged.group(2),None
        elif raw.startswith(('"','```','[')):
            value,error = _json_value(raw)
        else:
            value,error = raw,None
    if error:
        return None,error
    if not stable_value_valid(field,value):
        return None,{'reason':'field_type_invalid', 'detail':field,
                     'expected':'1..32 non-empty path strings' if field in PATH_LIST_FIELDS else 'one non-empty single-line string'}
    return value,None
