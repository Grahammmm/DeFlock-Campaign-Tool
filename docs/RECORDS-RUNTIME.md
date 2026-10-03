# Records runtime

The portable `campaign_tool` starter and the full `records run` extraction engine
have different requirements. Full intake is supported on **Linux with Python
3.11 or newer**, on a filesystem supporting descriptor-relative no-follow opens,
directory fsync and atomic `renameat2(RENAME_NOREPLACE)` publication. macOS and
Windows are not supported intake hosts. Do not replace this primitive with
check-then-rename: concurrent writers must never replace an existing receipt.

On an isolated Linux host, from a checkout pinned to the desired commit:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-records-parsers.txt
.venv/bin/python -B -m campaign_tool.records doctor --root /private-work/records
.venv/bin/python -B -m campaign_tool.records run --root /private-work/records --inbox /private-work/inbox
```

Use an existing private work filesystem outside the software checkout. The doctor
uses the nearest existing parent of the planned root, creates a temporary synthetic
probe there, and removes it. It does not create the records root, read originals,
inspect credentials, contact a service or send data. It tests initial publication,
refusal to replace a published directory, preservation of the original contents,
and cleanup. A symlink ancestor or unsupported filesystem fails with a stable
reason code. If the missing root will later be a new mount, rerun doctor after
mounting it. Every manual and unattended `records run` also probes before creating
its root, ledger or intake outputs. Library callers constructing `Pipeline` directly
must call `runtime.require(root)` themselves; this is not a cross-platform library
compatibility layer.

Exit 0 means the supported platform and tested filesystem primitives passed;
exit 2 means unsupported or unavailable. Parser package versions are reported
separately, with absent packages represented as null. This does not certify parser
coverage, OCR tools, legal review, hosted services or production readiness. Install
the pinned parser requirements for full format support. OCR additionally needs
Tesseract and Poppler; their existing extraction doctor checks those executables.

The `Records run slice` CI uses disposable Ubuntu 24.04 hosts, Python 3.11/3.12/3.13,
pinned parser packages and synthetic fixtures. It runs the real filesystem probes
and the end-to-end intake/publication/OCR suite with **zero skips**. This is the
supported validation path when developing on macOS. CI does not read a live
campaign or reuse development/production services. The runner container is a
separate scheduler runtime; building it alone is not validation of full records
intake.
