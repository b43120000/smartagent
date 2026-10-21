"""Read-only, bounded user-binding evidence. Never participates in admission."""
import hashlib
import json
import re
from .contracts import normalize_ui_text, ui_text_matches


def _sha(value):
    return hashlib.sha256(str(value).encode('utf-8', errors='replace')).hexdigest()[:16]


def _identity(text):
    # Only recognized transport labels and software-shaped request IDs are logged.
    # Never log arbitrary prompt fragments, URLs, attribute values or exception text.
    envelope=re.match(r'^\[WEBUI_SUBMIT_ID\]\s*WUS-[A-F0-9]{32}\s*\[/WEBUI_SUBMIT_ID\]\s*',text)
    if envelope:text=text[envelope.end():]
    kinds=('WEBAGENT_ACTIVE_REQUEST','WEBAGENT_FINAL_RENDER','AGENT_PROTOCOL_BOOTSTRAP',
           'AGENT_SESSION_ATTACH','WEBUI_SUBMIT_ID')
    match=re.match(r'^\[('+'|'.join(kinds)+r')\]',text)
    kind=match.group(1) if match else 'other'
    request_id=''
    if kind=='WEBAGENT_ACTIVE_REQUEST':
        try:
            data,_=json.JSONDecoder().raw_decode(text[match.end():].lstrip())
            value=data.get('request_id','') if isinstance(data,dict) else ''
            if isinstance(value,str) and re.fullmatch(r'WA-[A-Za-z0-9-]{1,76}',value):request_id=value
        except (ValueError,TypeError):pass
    return {'prompt_kind':kind,'request_id':request_id,'submit_marker_present':bool(envelope)}


def _comparison(actual,expected):
    left,right=normalize_ui_text(actual),normalize_ui_text(expected)
    compact_left,compact_right=re.sub(r'\s+','',left),re.sub(r'\s+','',right)
    def diff(a,b):
        if a==b:return None
        return next((i for i,(x,y) in enumerate(zip(a,b)) if x!=y),min(len(a),len(b)))
    return {'normalized_length':len(left),'normalized_sha':_sha(left),
            'text_match':ui_text_matches(actual,expected),'normalized_exact':left==right,
            'first_difference':diff(left,right),
            'first_nonwhitespace_difference':diff(compact_left,compact_right),
            'actual_is_prefix':bool(left) and right.startswith(left),
            'expected_is_prefix':bool(right) and left.startswith(right)}


def diagnose_user_binding(adapter,scope):
    expected=normalize_ui_text(scope.prompt_text)
    profile=adapter._active_profile()
    users=adapter.turns('user')
    baseline=set(scope.baseline_user_ids)
    from .base_adapter import _request_markers
    markers=set(scope.request_markers)
    records=[]
    text_matches=marker_matches=fresh_text_matches=0
    for turn in users:
        comparison=_comparison(turn.raw_text,expected)
        ordinal_new=turn.ordinal>scope.baseline_user_count
        digest_changed=(1<=turn.ordinal<=len(scope.baseline_user_content_digests)
                        and turn.content_digest!=scope.baseline_user_content_digests[turn.ordinal-1])
        newness=turn.structural_id not in baseline or ordinal_new or digest_changed
        overlap=bool(markers.intersection(_request_markers(turn.raw_text)))
        text_matches+=int(comparison['text_match'])
        fresh_text_matches+=int(comparison['text_match'] and newness)
        marker_matches+=int(overlap)
        record={'ordinal':turn.ordinal,'id_sha':_sha(turn.structural_id),
                'id_new':turn.structural_id not in baseline,'ordinal_new':ordinal_new,
                'content_changed_at_ordinal':bool(digest_changed),'marker_match':overlap,
                'identity_attribute_count':len(adapter.turn_identity_candidates(turn)),
                'raw_length':len(turn.raw_text),**_identity(normalize_ui_text(turn.raw_text)),**comparison}
        element=turn.element
        record['connected']=record['content_selector_count']=None
        if element is not None:
            try:record['connected']=bool(element.evaluate('el=>el.isConnected'))
            except Exception:record['element_read_failed']=True
            try:
                selector=profile.user_content_selector
                record['content_selector_count']=1 if selector==':scope' else len(element.query_selector_all(selector))
            except Exception:record['content_selector_read_failed']=True
        record['observation_reason']=(
            'element_detached' if record['connected'] is False else
            'content_selector_missing' if record['content_selector_count']==0 else
            'text_mismatch' if not comparison['text_match'] else
            'text_match_without_newness' if not newness else 'text_match_with_newness')
        records.append(record)
    if not users:reason='no_user_candidates'
    elif any(m.startswith('WUS-') for m in markers) and marker_matches!=1:reason='submit_marker_not_unique'
    elif not text_matches:reason='no_text_match'
    elif not fresh_text_matches:reason='text_match_without_newness'
    else:reason='text_match_present_check_binding_or_pairing'
    try:selector_count=adapter.page.locator(profile.user_turn_selector).count()
    except Exception:selector_count=None
    bound=scope.user_turn
    bound_connected=None
    if bound is not None and bound.element is not None:
        try:bound_connected=bool(bound.element.evaluate('el=>el.isConnected'))
        except Exception:pass
    # Prefer the best textual candidate plus the last few mounted turns. Ordering
    # below affects diagnostics only, never the provider's acceptance algorithm.
    selected=records[-5:]
    if records:
        best=max(records,key=lambda r:(r['text_match'],r['marker_match'],r['first_difference'] or 0))
        if best not in selected:selected=[best]+selected
    return {'schema':'USER_BINDING_DIAGNOSTIC_V1','scope_id':scope.scope_id,
            **_identity(expected),'diagnostic_reason':reason,
            'expected_normalized_length':len(expected),'expected_normalized_sha':_sha(expected),
            'baseline_user_count':scope.baseline_user_count,'observed_user_count':len(users),
            'selector_match_count':selector_count,'text_match_count':text_matches,
            'fresh_text_match_count':fresh_text_matches,'marker_match_count':marker_matches,
            'bound_user_present':bound is not None,'bound_element_connected':bound_connected,
            'candidate_count':len(records),'candidates_omitted':len(records)-len(selected),
            'candidates':selected}
