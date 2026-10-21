"""Calibrated attachment observations and request-owned browser downloads."""
import hashlib
import time
from pathlib import Path

from .file_config import load_files
from .attachment_probe import observed_send_transition, observed_busy


def unique(page, selector, *, visible=True):
    items = page.locator(selector)
    matches = [items.nth(i) for i in range(items.count())
               if not visible or items.nth(i).is_visible()]
    if len(matches) != 1:
        raise ValueError('file_dom_not_unique:' + selector + ':' + str(len(matches)))
    return matches[0]


def attachment_state(adapter, names, *, config=None):
    config = config or load_files(adapter.provider_name)
    if not config.get('attachment_verified'):
        return None
    page = adapter.page
    chips = [node for node in page.locator(config['chip']).all() if node.is_visible()]
    # Only composer chips qualify; never reuse an attachment in conversation history.
    chips = [node for node in chips if not any(node.evaluate('(el,s)=>!!el.closest(s)', selector)
             for selector in (adapter.profile.user_turn_selector, adapter.profile.assistant_turn_selector))]
    busy = []
    for chip in chips:
        if chip.evaluate('(el,s)=>el.matches(s)', config['busy']):
            busy.append(chip)
        busy.extend(node for node in chip.locator(config['busy']).all() if node.is_visible())
    informational = getattr(adapter, 'is_informational_attachment_notice', lambda node: False)
    errors = [node.inner_text() for node in page.locator(config['error']).all() if node.is_visible()
              and not informational(node)
              and not any(node.evaluate('(el,s)=>!!el.closest(s)', selector)
                  for selector in (adapter.profile.user_turn_selector, adapter.profile.assistant_turn_selector))]
    records = []
    for node in chips:
        descriptor = node.evaluate('el=>[el.innerText,el.getAttribute("aria-label"),el.getAttribute("title"),el.getAttribute("data-file-name")].filter(Boolean).join(" ")')
        attributes=node.evaluate("""el=>{const values={};for(const name of ['data-state','data-upload-state','aria-busy'])values[name]=el.getAttribute(name);const invalid=el.getAttribute('aria-invalid')==='true'||!!el.querySelector('[aria-invalid="true"]');const terminal=Array.from(el.querySelectorAll('[data-state],[data-upload-state]')).concat([el]).some(item=>/^(error|failed|invalid|rejected|cancelled|canceled)$/i.test(item.getAttribute('data-upload-state')||item.getAttribute('data-state')||''));return {values,invalid,terminal};}""")
        records.append(dict(descriptor=descriptor, removable=bool(node.locator(config['remove']).count()),
                            processing=bool(busy), error=bool(errors) or attributes['invalid'] or attributes['terminal'], explicit_complete=False, progress=[],
                            attributes=attributes['values']))
    selected_inputs = page.evaluate("""selector=>Array.from(document.querySelectorAll(selector)).flatMap(i=>Array.from(i.files||[]).map(f=>f.name))""", config['input'])
    return dict(text='\n'.join(item['descriptor'] for item in records), chips=[item['descriptor'] for item in records],
                chip_records=records, busy=['uploading'] * len(busy), upload_ring_count=len(busy), alerts=errors, dialogs=[], selected_inputs=selected_inputs)


def wait_attachment(adapter, filename, *, timeout=90, config=None):
    started = None; saw_busy = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if adapter.page.evaluate('()=>window.__smartAgentFilePicker?.action==="cancel"'):
            raise InterruptedError('calibration_cancelled')
        blocked = adapter.blocking_transport_error()
        if blocked:
            raise RuntimeError('provider_blocked:' + blocked['reason'])
        state = attachment_state(adapter, [filename], config=config)
        if state is None:
            raise ValueError('attachment_state_not_calibrated')
        if state['alerts']:
            raise ValueError('attachment_upload_rejected:' + '|'.join(state['alerts']))
        records = state['chip_records']
        if any(record.get('error') for record in records):
            raise ValueError('attachment_upload_rejected:scoped_error')
        saw_busy = saw_busy or bool(state['busy']) or observed_busy(adapter.page, filename)
        record_ready = len(records) == 1 and filename in records[0]['descriptor'] and records[0]['removable']
        if state['selected_inputs'] and filename not in state['selected_inputs']:
            raise ValueError('attachment_input_drift')
        probe=(config or load_files(adapter.provider_name)).get('attachment_probe',{})
        explicit_ready=(probe.get('completion') == 'explicit_ready' and not state['busy'] and
                        record_ready and records[0]['attributes'].get(probe.get('ready_attribute')) == probe.get('ready_value'))
        positive = ((probe.get('completion') == 'busy_idle' and saw_busy and not state['busy']) or
                    explicit_ready or
                    (probe.get('completion') == 'send_transition' and observed_send_transition(adapter.page)))
        ready = record_ready and positive
        if ready:
            started = started or time.monotonic()
            if time.monotonic() - started >= 1.5:
                return state
        else:
            started = None
        adapter.page.wait_for_timeout(150)
    raise TimeoutError('attachment_not_ready:no_automatic_resubmit')


def download_owned(adapter, scope, destination, *, timeout=45, expected_filename='', config=None):
    config = config or load_files(adapter.provider_name)
    if not config.get('download_verified'):
        raise ValueError('download_dom_not_verified')
    if scope is None or scope.user_turn is None:
        raise ValueError('download_requires_bound_request')
    turn = adapter.latest_owned_assistant(scope)
    if turn is None:
        raise ValueError('download_requires_owned_fresh_assistant')
    root = turn.element
    controls = root.query_selector_all(config['download'])
    choices = [node for node in controls if node.is_visible()]
    if len(choices) != 1:
        raise ValueError('download_control_not_unique_in_owned_answer')
    path = Path(destination).resolve()
    if path.exists():
        raise FileExistsError('download_refuses_overwrite:' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with adapter.page.expect_download(timeout=int(timeout * 1000)) as pending:
        try:
            choices[0].click(timeout=3000)
        except Exception as exc:
            if 'intercepts pointer events' not in str(exc):
                raise
            # Some artifact cards layer their preview button over the download
            # action. Keyboard activation reaches that same real control.
            choices[0].focus()
            choices[0].press('Enter')
    download = pending.value
    failure = download.failure()
    if failure:
        raise RuntimeError('browser_download_failed:' + failure)
    if expected_filename and download.suggested_filename != expected_filename:
        raise ValueError('download_filename_mismatch:' + download.suggested_filename)
    download.save_as(path)
    data = path.read_bytes()
    if not data:
        raise ValueError('empty_download')
    return dict(path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                suggested_filename=download.suggested_filename, method='calibrated_owned_browser_download',
                owned_assistant_id=adapter.durable_turn_id(turn))
