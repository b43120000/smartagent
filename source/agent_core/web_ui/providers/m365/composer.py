"""Microsoft editor serialization without changing the supplied prompt."""
import hashlib
from ...contracts import ComposerSnapshot, ComposerValidation, normalize_ui_text, ui_text_matches


def read_composer_snapshot(composer):
    if composer is None:
        return ComposerSnapshot(primary_text="", semantic_text="")
    primary = str(composer.evaluate("el => 'value' in el ? el.value : (el.innerText || el.textContent || '')") or "")
    semantic = str(composer.evaluate(r"""el => {
        if ('value' in el) return el.value || '';
        const clone = el.cloneNode(true);
        for (const br of clone.querySelectorAll('br')) br.replaceWith('\n');
        for (const block of clone.querySelectorAll('p, div')) {
            if (block.nextSibling) block.append('\n');
        }
        return clone.textContent || '';
    }""") or "")
    return ComposerSnapshot(primary_text=primary, semantic_text=semantic)


def validate_composer_prompt(prompt, snapshot):
    view = ""
    observed = snapshot.primary_text
    for name, text in (("primary", snapshot.primary_text), ("semantic", snapshot.semantic_text)):
        if ui_text_matches(text, prompt):
            view, observed = name, text
            break
    expected, observed = normalize_ui_text(prompt), normalize_ui_text(observed)
    sha = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ComposerValidation(matched=bool(view), matched_view=view,
        expected_length=len(expected), observed_length=len(observed),
        expected_sha256=sha(expected), observed_sha256=sha(observed))
