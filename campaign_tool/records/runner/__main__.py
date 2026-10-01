"""Offline-only preparatory command. No live provider, scheduling or promotion."""
import argparse
import json
from .adapters import OfflineExporter,LegacyIntakeBackend
from .core import run,observed_runtime

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',required=True);p.add_argument('--control-root',required=True)
    p.add_argument('--mail-root',required=True);p.add_argument('--export-manifest',required=True)
    p.add_argument('--intake-output',required=True);p.add_argument('--image-digest',required=True)
    a=p.parse_args()
    try:
        result=run(a.control_root,a.profile,OfflineExporter(a.mail_root,a.export_manifest),
                   LegacyIntakeBackend(a.mail_root,a.intake_output),{},
                   runtime_provider=lambda:observed_runtime(a.image_digest))
    except Exception:
        p.exit(2,'records runner: failed (redacted); inspect private control events\n')
    print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
