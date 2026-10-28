"""Build a wheel whose installed code is bound to a clean source commit."""
import json
from pathlib import Path
import sys
from setuptools import setup
from setuptools.command.build_py import build_py


class ProvenanceBuild(build_py):
    def run(self):
        root = Path(__file__).resolve().parent
        sys.path.insert(0, str(root))
        from campaign_tool.records.release_manifest import package_files, source_manifest
        manifest = source_manifest(root)
        if manifest["source_dirty"]:
            raise RuntimeError("release wheels require a clean source tree; commit reviewed source first")
        super().run()
        if package_files(Path(self.build_lib) / "campaign_tool") != manifest["package_files"]:
            raise RuntimeError("built package differs from the committed source inventory")
        target = Path(self.build_lib) / "campaign_tool" / "records"
        (target / "_release_dependencies.txt").write_bytes((root / "requirements-records-test.txt").read_bytes())
        (target / "_release_manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


setup(cmdclass={"build_py": ProvenanceBuild})
