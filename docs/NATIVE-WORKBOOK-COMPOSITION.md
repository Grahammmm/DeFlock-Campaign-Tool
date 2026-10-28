# Optional native workbook composition

Trusted startup can explicitly pass a NativeWorkbookPolicy to Pipeline,
install_extraction_validator and ExtractionStageAdapter. There is no
record-selected module, automatic policy enrollment or runtime installation.
The default installed policy still refuses XLSX and Pillow acceptance.

The records run's extract stage (`run_stages extract`) is such a trusted startup.
`--workbook-policy PATH` must come from the operator's root-owned stage plan,
never from a record. It is loaded only through `load_native_workbook_policy`, so
pinned digest, 0400 mode and a single link are required. Any other file fails
the stage with `EXTRACT_WORKBOOK_POLICY_INVALID`, and there is no fallback. With
the policy loaded, `Pipeline.stage_extract` passes a workbook receipt whose only
issue is the reader hold to enrollment. The installed check then qualifies it
under the policy or refuses it, and a refusal is held once as
`WORKBOOK_POLICY_REQUIRED`. The TEST stage plan does not set the flag.

The loader accepts only the exact pinned read-only workbook accounting helper on
AgentOrange. It compiles verified local bytes rather than cached bytecode. The
helper remains private and is neither included in the wheel nor admitted to this
public repository. Byte verification does not grant owner policy authorization;
reviewed installed authority remains a separate activation gate.

The caller preflights every ZIP member and XML byte budget before invoking the
helper through an open read-only ZipFile. Both the legacy v3 qualifier and native
accounting must have empty gaps. Scope labels and counts alone are insufficient.
Formula/order/package qualification holds remain even when accounting has no
gaps. Limits remain 16 MiB per part, 64 MiB expanded package, 1024 sheets, and the
existing 8 MiB source / 10000-unit acceptance limits; this does not qualify a
large campaign workbook from a reported row count.

The adapter independently reruns the installed openpyxl reader on original CAS
bytes and checks exact units, component versions and source-bound totals.
openpyxl and et-xmlfile distribution metadata and source files enter the runtime
binding. Row text uses the existing intake.folder.js serializer, preserving cell
list order, spacing and the literal '[]' representation for empty serialized
rows. The coverage denominator is serialized native rows, not informative text
or an expanded worksheet grid. Empty sheets remain explicit sheet units.
Parser-created sparse placeholders and explicit blank cells without structured
source dispositions are held. Formula-bearing workbooks remain held.

Hidden/veryHidden states and the complete structural accounting remain in the
content envelope. Boolean false review flags mean no approval granted. Native
qualification never grants semantic, visual, legal or independent approval.
The known cached-values/object reader issue is retained and only reconciled for
this narrow native-row policy; other extraction issues remain blocking.

Completed older extraction states are not automatically reused under these new
versions. Controlled extract/dependent invalidation and publication authority
revocation are deployment prerequisites; historical receipts remain immutable.
Synthetic helper stubs and testing_runner results are explicitly test-only.
Actual pinned private-helper composition must be qualified locally on
AgentOrange, with trusted non-test preservation and authority verified separately.
