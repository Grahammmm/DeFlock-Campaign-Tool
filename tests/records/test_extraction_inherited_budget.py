"""Actual bounded native-text child under stricter launch resource ceilings."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform.startswith('linux'), 'bounded parser runtime is Linux')
class InheritedExtractionBudgetTests(unittest.TestCase):
    def test_native_text_respects_parent_caps_and_records_effective_memory(self):
        import resource
        with tempfile.TemporaryDirectory(prefix='synthetic-inherited-budget-') as temporary:
            root = Path(temporary).resolve()
            source = root/'fictional.txt'
            raw = b'Fictional native text under a stricter parent ceiling.\n'
            source.write_bytes(raw);source.chmod(0o400)
            output = root/'output';output.mkdir(mode=0o700)
            def caps():
                resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
                resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
            code = ("import json,sys; from campaign_tool.records import extraction_routes as r; "
                    "x=r.extract(sys.argv[1],sys.argv[2],sys.argv[3],form='txt',timeout=20); print(json.dumps(x))")
            result = subprocess.run([sys.executable,'-B','-c',code,str(source),hashlib.sha256(raw).hexdigest(),str(output)],
                                    capture_output=True,text=True,timeout=30,preexec_fn=caps)
            self.assertEqual(result.returncode,0,result.stderr)
            receipt=json.loads(result.stdout)
            self.assertEqual(receipt['status'],'complete',receipt['issues'])
            self.assertEqual(receipt['units'][0]['text'],raw.decode().strip())
            metadata=json.loads((Path(receipt['run_path'])/'derived/digest.json').read_text())
            self.assertEqual(metadata['effective_limits']['memory_bytes'],1024**3)
            self.assertEqual(source.read_bytes(),raw)
            self.assertEqual(receipt['review_status'],'not_reviewed')
            self.assertFalse(receipt['original_changed'])


if __name__=='__main__':
    unittest.main()
