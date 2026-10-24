"""Fresh-process, synthetic-only import probe; never runs the test suite."""
from contextlib import ExitStack
from pathlib import Path
import runpy
import sys
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True

with ExitStack() as stack:
    probes = [stack.enter_context(mock.patch(name, side_effect=AssertionError('import side effect: ' + name)))
              for name in ('sqlite3.connect', 'pathlib.Path.mkdir', 'pathlib.Path.write_bytes',
                           'pathlib.Path.write_text', 'tempfile.mkdtemp', 'shutil.copytree')]
    runpy.run_path(str(REPO / 'tests/test_comprehension_contract.py'), run_name='comprehension_import_probe')
    for probe in probes:
        probe.assert_not_called()
print('PASS pre-import isolation: no database or fixture write')
