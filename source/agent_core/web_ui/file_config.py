"""Validated file DOM namespace, separate from immutable dialogue profiles."""
from datetime import datetime, timezone
import json
import uuid

SELECTORS = {'entry', 'upload_action', 'input', 'chip', 'busy', 'error', 'remove', 'download'}
PROOFS = {'entry_verified', 'upload_verified', 'attachment_verified', 'attachment_pending', 'attachment_inventory_verified', 'download_verified', 'roundtrip'}
STRUCTURED = {'attachment_probe'}


def validate_file_config(config):
    from .profile_store import _validate_selector
    if not isinstance(config, dict) or set(config) - SELECTORS - PROOFS - STRUCTURED:
        raise ValueError('manual_file_invalid_fields')
    for name in SELECTORS & config.keys():
        _validate_selector(config[name], field='file_' + name)
    for name in PROOFS & config.keys():
        if config[name] is not True:
            raise ValueError('manual_file_invalid_proof:' + name)
    if 'attachment_probe' in config:
        probe=config['attachment_probe']
        if not isinstance(probe,dict) or probe.get('completion') not in {'busy_idle','explicit_ready','send_transition'}:
            raise ValueError('manual_file_invalid_attachment_probe')
        if probe.get('completion')=='explicit_ready' and (not isinstance(probe.get('ready_attribute'),str) or not isinstance(probe.get('ready_value'),str)):
            raise ValueError('manual_file_explicit_ready_requires_observed_value')
    dependencies = {'entry_verified': {'entry'}, 'upload_verified': {'input'},
                    'attachment_verified': {'chip', 'remove', 'upload_verified', 'attachment_probe', 'attachment_inventory_verified'},
                    'download_verified': {'download'},
                    'roundtrip': {'entry_verified', 'upload_verified', 'attachment_verified', 'download_verified'}}
    for proof, required in dependencies.items():
        if config.get(proof) and not required.issubset(config):
            raise ValueError('manual_file_missing_dependencies:' + proof)
    return config


def load_files(provider):
    from .dom_config import load_dom_config
    return (load_dom_config(provider) or {}).get('files', {})


def web_file_capabilities(provider):
    """Publish live, provider-local file availability; partial proofs stay disabled."""
    from .dom_config import load_dom_config, ELEMENT_FIELDS, CAPABILITY_KEYS
    reason = 'file_roundtrip_not_verified'
    enabled = False
    try:
        document = load_dom_config(provider) or {}
        files = document.get('files', {})
        dialogue_ready = (set(ELEMENT_FIELDS).issubset(document.get('elements', {}))
                          and set(CAPABILITY_KEYS).issubset(document.get('capabilities', {})))
        enabled = bool(dialogue_ready and not files.get('attachment_pending') and all(
            files.get(key) is True for key in (
                'entry_verified', 'upload_verified', 'attachment_inventory_verified',
                'attachment_verified', 'download_verified', 'roundtrip')))
        if enabled:
            reason = 'file_roundtrip_verified'
        elif not dialogue_ready:
            reason = 'dialogue_calibration_incomplete'
    except Exception:
        reason = 'calibration_unavailable'
    return {'schema': 'WEB_UI_FILE_CAPABILITIES_V1', 'provider': provider,
            'mode': 'text_and_files' if enabled else 'text_only',
            'browser_upload': enabled, 'browser_download': enabled, 'reason': reason}


def render_web_file_capabilities(capabilities):
    return ('\n[WEB_UI_FILE_CAPABILITIES]\n'
            + json.dumps(capabilities, ensure_ascii=False, separators=(',', ':'))
            + '\nThese Runtime-owned capabilities override historical/default file-transfer claims. '
            'When browser_upload or browser_download is false, do not plan upload_file, upload_files, '
            'download_artifact, or project_sync FULL_BUNDLE/DELTA. Use local read_file, list_directory, '
            'query_project, write_file, run_command and project_sync INDEX_ONLY as appropriate; '
            'local tools remain available within authorization. Return generated text/code through '
            'the text protocol and local write tools. Do not ask the user to upload local paths. '
            'If the goal specifically requires browser file transfer, report that stages 11-15 '
            'must be completed rather than claiming delivery. When both are true, use the normal '
            'file workflow with all existing authorization, freshness and verification checks. '
            'This policy concerns browser attachments/artifact downloads only; other authorized '
            'local, Telegram and Google Drive tools retain their own capabilities.\n'
            '[/WEB_UI_FILE_CAPABILITIES]')


def save_files(provider, settings, *, root=None):
    from .dom_config import load_dom_config, config_path
    from .profile_store import _atomic_write
    current = load_dom_config(provider, root=root)
    if not current:
        raise ValueError('calibrate_composer_first')
    files = dict(current.get('files', {}))
    changed = {key for key in SELECTORS & settings.keys() if files.get(key) != settings[key]}
    if changed:
        files.pop('roundtrip', None)
        if changed & {'input', 'upload_action', 'entry'}:
            files.pop('upload_verified', None)
            files.pop('attachment_verified', None)
        if changed & {'chip', 'busy', 'error', 'remove'}:
            files.pop('attachment_verified', None)
            files.pop('attachment_inventory_verified', None)
            files.pop('attachment_probe', None)
        if 'download' in changed:
            files.pop('download_verified', None)
    files.update(settings)
    if settings.get('attachment_pending'):
        files.pop('attachment_verified', None)
    validate_file_config(files)
    document = {**current, 'files': files, 'revision': uuid.uuid4().hex,
                'updated_at': datetime.now(timezone.utc).isoformat()}
    path = config_path(provider, root=root)
    _atomic_write(path.parent / 'dom_config.history' / (uuid.uuid4().hex + '.json'), current)
    _atomic_write(path, document)
    return path
