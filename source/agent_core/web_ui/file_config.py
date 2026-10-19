"""Validated file DOM namespace, separate from immutable dialogue profiles."""
from datetime import datetime, timezone
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
