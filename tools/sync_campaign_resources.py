"""Check or copy an explicit allowlist of public resources into the wheel package."""
import argparse
from pathlib import Path

RESOURCES = (
    "data/agencies/us-ca.json",
    "schemas/agency-seed.schema.json",
    "schemas/law-package.schema.json",
    "jurisdictions/us-ca/package.json",
    "templates/records-request.md",
)


def sync(root, write=False):
    root = Path(root).resolve()
    failures = []
    for name in RESOURCES:
        source = root / name
        target = root / "campaign_tool" / "_resources" / name
        for candidate in (source, target):
            if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents if p != root and root in p.parents):
                raise ValueError("symlink resource path: " + name)
        original = source.read_bytes()
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original)
        if not target.is_file() or target.read_bytes() != original:
            failures.append(name)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Copy the five public canonical resources; default is read-only")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    failures = sync(root, args.write)
    for name in failures:
        print("Resource differs or is absent: " + name)
    if not failures:
        print("Five bundled campaign resources match their canonical public sources.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
