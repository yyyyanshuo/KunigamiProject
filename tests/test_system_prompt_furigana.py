"""System notices must bypass conversion in both annotation entrypoints."""
import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SystemPromptFuriganaTests(unittest.TestCase):
    def test_system_notices_return_before_loading_converter(self):
        for filename in ('core/utils.py', 'app.py'):
            tree = ast.parse((ROOT / filename).read_text(encoding='utf-8-sig'))
            function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_add_furigana_to_japanese')
            namespace = {}
            exec(compile(ast.Module(body=[function], type_ignores=[]), filename, 'exec'), namespace)
            annotate = namespace[function.name]
            for text in ('（系统提示：今日予定を確認してください。）', '  （系统提示：第一行\n第二行/第三行）  ', '', None):
                with self.subTest(filename=filename, text=text):
                    # No converter dependencies are present: any conversion would fail.
                    self.assertEqual(annotate(text), text)


if __name__ == '__main__':
    unittest.main()
