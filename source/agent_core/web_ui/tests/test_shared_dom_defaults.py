"""Public DOM mappings are fallbacks; private mappings remain authoritative."""
from pathlib import Path
import tempfile
import unittest

from ..dom_config import config_path, load_dom_config, save_composer


class SharedDOMDefaultsTests(unittest.TestCase):
    def test_four_public_defaults_are_valid_and_have_no_run_records(self):
        for provider in ('chatgpt', 'gemini', 'claude', 'm365'):
            doc = load_dom_config(provider)
            self.assertEqual(doc['revision'], 'shared-default-20261021')
            self.assertNotIn('created_at', doc)
            for cap in doc['capabilities'].values():
                self.assertFalse({'response_id', 'rounds', 'trace', 'response_samples'} & cap.keys())
            self.assertNotIn('file', doc.get('files', {}).get('attachment_probe', {}))

    def test_local_precedence_broken_local_rejected_and_saves_stay_local(self):
        source = config_path('m365').with_name('dom_config.default.json')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            local = config_path('m365', root=root)
            local.parent.mkdir(parents=True)
            default = local.with_name('dom_config.default.json')
            default.write_bytes(source.read_bytes())
            before = default.read_bytes()
            self.assertEqual(load_dom_config('m365', root=root)['revision'], 'shared-default-20261021')
            save_composer('m365', 'https://m365.cloud.microsoft', '#my-composer', {}, root=root)
            self.assertEqual(load_dom_config('m365', root=root)['elements']['composer']['selector'], '#my-composer')
            self.assertEqual(default.read_bytes(), before)
            local.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'schema_mismatch'):
                load_dom_config('m365', root=root)

    def test_other_does_not_adopt_a_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = config_path('other_' + 'a' * 64, root=root)
            path.parent.mkdir(parents=True)
            path.with_name('dom_config.default.json').write_text('{}', encoding='utf-8')
            self.assertIsNone(load_dom_config('other_' + 'a' * 64, root=root))


if __name__ == '__main__':
    unittest.main()
