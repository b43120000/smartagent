"""Read-only checks shared with the actual calibration/runtime DOM algorithms."""
from .contracts import normalize_ui_text, ui_text_matches
from .message_text import read_message_text, read_answer_text


def check_component_selection(page, stage, probe, selection):
    # Import lazily: component_calibration also owns the selection polling loop.
    from .component_calibration import candidate_adapter, content_text, send_control, user_root
    from .dom_config import load_dom_config
    from .provider_registry import provider_from_url
    checks = []

    def check(label, passed, detail):
        checks.append(dict(label=label, passed=bool(passed), detail='已通過。' if passed else detail))
        if not passed:
            raise ValueError(detail)

    handle = page.evaluate_handle('() => window.__smartAgentComponentPicker.getSelectedElement()')
    try:
        element = handle.as_element()
        check('元件狀態', element is not None and element.is_visible() and
              element.evaluate('el=>el.isConnected'), '元件須仍在頁面上且可見；若已更新，請重新選取。')
        selector = selection['selector']
        if stage==9:
            check('阻擋視窗',bool(element.inner_text().strip()),'請選實際出現、有訊息內容的阻擋視窗。')
            document=load_dom_config(provider_from_url(page.url))
            roots=[entry['selector'] for name,entry in document['elements'].items() if name in {'user_turn','assistant_turn'}]
            check('非對話內容',not element.evaluate('(el,selectors)=>selectors.some(s=>!!el.closest(s)||!!el.querySelector(s))',roots),
                  '請只選錯誤提示框，不要包含使用者訊息、模型回答或整個對話區。')
            nodes=page.locator(selector)
            visible=[n for n in nodes.all() if n.is_visible()]
            check('視窗定位',len(visible)==1 and visible[0].evaluate('(el,chosen)=>el===chosen',element),'定位須唯一對應目前的阻擋視窗。')
            return dict(passed=True,checks=checks)
        if stage==7:
            button=send_control(page,selector)
            check('停止按鈕',button.evaluate('(el,chosen)=>el===chosen',element) and candidate_adapter(page,'stop',selector).generation_active(),'停止按鈕須可見且可用；若已完成回覆，請重新執行。')
            return dict(passed=True,checks=checks)
        if stage==6:
            adapter=candidate_adapter(page,'assistant_content',selector)
            matches=[t for t in adapter.turns('assistant') if probe['marker'] in t.raw_text and 'SmartAgent DOM calibration' not in t.raw_text]
            check('本輪回答',len(matches)==1,'請選本輪唯一的模型回答。')
            root=matches[0].element
            check('正文範圍',root.evaluate('(el,chosen)=>el===chosen||el.contains(chosen)',element),'正文須位於本輪回答外框內。')
            actual=adapter.extract_final_text(matches[0])
            check('完整正文',probe['marker'] in actual and probe.get('end_marker',probe['marker']) in actual and ui_text_matches(actual,read_answer_text(element)),'請選包含完整回答的正文區，不含其他操作元件。')
            return dict(passed=True,checks=checks)
        if stage == 2:
            button = send_control(page, selector)
            check('按鈕定位', button.evaluate('(el,chosen)=>el===chosen', element), '定位須唯一對應目前選取的可用送出按鈕。')
            checks.append(dict(label='後續驗證', passed=None,
                               detail='目前只確認按鈕型別與可用性；仍須功能測試確認能送出本輪訊息。'))
            return dict(passed=True, checks=checks)
        if stage in (3, 4):
            document = load_dom_config(provider_from_url(page.url))
            user_selector = selector if stage == 3 else document['elements']['user_turn']['selector']
            root = user_root(page, user_selector, probe)
            text = read_message_text(element)
            check('預期內容', normalize_ui_text(probe['prompt']) in normalize_ui_text(text), '請選本輪使用者訊息，必須包含完整問題文字。')
            check('單則訊息範圍', text.count(probe['marker']) == probe['prompt'].count(probe['marker']), '選取範圍混入其他訊息或重複內容，請縮小範圍。')
            if stage == 3:
                check('本輪定位', root.evaluate('(el,chosen)=>el===chosen', element), '目前點到的元件須是本輪訊息，不能是其他歷史訊息。')
                turns = candidate_adapter(page, 'user_turn', selector).turns('user')
                matching = [turn for turn in turns if ui_text_matches(turn.raw_text, probe['prompt'])]
                check('執行時讀取', len(matching) == 1 and matching[0].element.evaluate('(el,chosen)=>el===chosen', element), '執行時無法唯一辨認本輪使用者訊息；請調整外框。')
                attrs = matching[0].stable_attributes
                checks.append(dict(label='識別方式', passed=None,
                    detail='已找到識別欄位，唯一性待確認：' + ', '.join(name for name, _ in attrs) if attrs else
                           '目前依訊息順序辨認；仍須兩輪功能驗證，尚未證明可穩定重用。'))
            else:
                check('正文定位', root.evaluate('(el,chosen)=>el===chosen||el.contains(chosen)', element) and
                      ui_text_matches(text, probe['prompt']) and ui_text_matches(content_text(root, selector), probe['prompt']),
                      '請選本輪使用者訊息內的完整正文，不含外框或其他內容。')
        else:
            adapter = candidate_adapter(page, 'assistant_turn', selector)
            chosen = adapter._turn_ref(element, 'assistant', 0)
            check('預期回答', probe['marker'] in chosen.raw_text and 'SmartAgent DOM calibration' not in chosen.raw_text,
                  '請選含本輪標記的模型回答；不能包含使用者問題。')
            if not chosen.stable_attributes:
                check('訊息識別', False, '沒有可用的訊息識別資訊；請選完整回答外框。若已選外框，可能需要更新此網站適配規則。')
            checks.append(dict(label='訊息識別', passed=None, detail='已找到識別欄位，唯一性待確認'))
            turns = adapter.turns('assistant')
            matching = [turn for turn in turns if probe['marker'] in turn.raw_text and 'SmartAgent DOM calibration' not in turn.raw_text]
            check('本輪唯一定位', len(matching) == 1 and matching[0].element.evaluate('(el,selected)=>el===selected', element),
                  '定位無法唯一對應目前選取的本輪回答，可能命中多層容器或重複訊息。')
            candidates = adapter.turn_identity_candidates(chosen)
            checks.append(dict(label='識別來源', passed=None,
                               detail=('候選欄位：' if candidates else '目前只有類型／狀態欄位，不能視為唯一訊息 ID：') +
                               ', '.join(name for name, _ in (candidates or chosen.stable_attributes))))
        checks.append(dict(label='後續驗證', passed=None, detail='仍須送出下一輪，驗證定位可重用且不會讀到舊回答。'))
        return dict(passed=True, checks=checks)
    except Exception as exc:
        # Selection errors remain in the panel; they must not crash the BAT.
        reasons = {'user_container_includes_other_turns':'定位包含其他訊息或重複內容，請調整為單則訊息外框。',
                   'user_selector_also_matches_assistant':'定位也命中模型回答，請重新選取使用者訊息外框。',
                   'new_user_turn_not_unique':'本輪使用者訊息無法唯一定位，請調整外框。'}
        if not checks or checks[-1]['passed'] is not False:
            checks.append(dict(label='定位與解析', passed=False, detail=reasons.get(str(exc), str(exc))))
        return dict(passed=False, checks=checks)
    finally:
        handle.dispose()
