"""One plain sentence per stage, needs-G and outcome code the run engine emits (DFCON17-03).

A fixed table: nothing here is read from a run report. ``explain`` is shared by the run detail page and any other
console page that shows a code. A code with no entry is shown as itself with "no description yet", never hidden.
"""
import re

from . import console_layout

CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")  # the run report schema's code grammar
NO_DESCRIPTION = "no description yet"

CODES = {
    # run_orchestrator: statuses it sets itself
    "STAGE_DISABLED": "This stage is switched off for this run.",
    "NOT_REACHED": "The run ended before this stage began.",
    "UPSTREAM_FAILED": "An earlier stage failed, so this stage was skipped.",
    "RUN_DEADLINE_REACHED": "The run hit its time limit before this stage could finish.",
    "RUN_INTERRUPTED": "The run was interrupted before this stage began.",
    "STAGE_SPAWN_FAILED": "The stage could not be started.",
    "STAGE_TIMEOUT": "The stage took longer than its time limit and was stopped.",
    "STAGE_CONTAINER_TIMEOUT": "The sandbox running the stage hit its time limit and was stopped.",
    "STAGE_LEFT_PROCESSES": "The stage left background processes running, so it was not trusted.",
    "STAGE_RESULT_INVALID": "The stage returned a result the engine could not read.",
    "STAGE_INTERNAL_ERROR": "The stage hit an unexpected internal error.",
    "STAGE_CONFIG_INVALID": "The stage's settings are not valid, so it did not run.",
    "STUB_STAGE_BACKEND": "A test stand-in ran this stage; nothing real was done.",
    "SANDBOX_MARKER_MISSING": "The project folder has no sandbox marker, so the run refused to start.",
    "SANDBOX_MARKER_UNSAFE": "The project folder's sandbox marker is not safe, so the run refused to start.",
    "NOTIFY_PENDING": "The summary notification had not been sent when this report was written.",
    "NOTIFY_FAILED": "The summary notification could not be sent.",
    "NOTIFY_HELPER_TIMEOUT": "The notification helper took too long, so the summary was not sent.",
    # sync, delta, adoption
    "SYNC_FAILED": "Syncing the mail export failed.",
    "SYNC_EXPORT_MISSING": "The mail export to read was not found.",
    "SYNC_STATUS_UNREADABLE": "The mail export was read, but its sync status file could not be read.",
    "SYNC_OFFLINE_SNAPSHOT": "Mail was read from an offline copy; nothing new was fetched.",
    "LEDGER_WAL_PENDING": "The records ledger was busy, so this step was skipped for now.",
    "DELTA_REFUSED": "The check for new mail was refused.",
    "DELTA_FAILED": "The check for new mail failed.",
    "DELTA_BLOCKING_CONFLICTS": "New mail conflicts with records already held and needs a person to resolve it.",
    "ADOPTION_NOT_CONFIGURED": "Mail adoption is not set up, so nothing was adopted.",
    "ADOPT_BACKUP_FAILED": "The ledger backup before adoption failed, so nothing was adopted.",
    "ADOPT_BACKUP_SKIPPED": "No ledger backup was needed because nothing was going to be written.",
    "ADOPTION_REFUSED": "Adopting new mail was refused.",
    "ADOPTION_BLOCKED": "Adoption stopped on conflicts or gaps that need a person.",
    "ADOPTION_FAILED": "Adopting new mail failed.",
    "ADOPTION_CODED_GAPS": "Some messages could not be adopted and were recorded as known gaps.",
    "ADOPTION_IDENTITY_CONFLICT": "A message conflicts with the identity of one already adopted.",
    "ADOPTION_UNACCEPTED_GAP": "A gap in the mail has not been accepted by a person yet.",
    "ADOPTION_RECEIPT_GAPS": "Some mail receipts are incomplete.",
    # portal
    "PORTAL_ALLOWLIST_MISSING": "The approved portal list is missing, so no portal was contacted.",
    "PORTAL_ALLOWLIST_EXPIRED": "The approved portal list has expired, so no portal was contacted.",
    "PORTAL_ALLOWLIST_INVALID": "The approved portal list is not valid, so no portal was contacted.",
    "PORTAL_ALLOWLIST_UNSAFE": "The approved portal list is not safe to use, so no portal was contacted.",
    "PORTAL_INTAKE_FAILED": "Portal notices could not be recorded.",
    "PORTAL_NOTICE_UNREADABLE": "A portal notice could not be read.",
    "PORTAL_HTTP_404": "An approved portal link returned not found.",
    "PORTAL_FETCH_BLOCKED": "An approved portal link could not be fetched.",
    "PORTAL_HOST_NOT_ALLOWLISTED": "A portal link points at a site that is not on the approved list.",
    "PORTAL_HOST_REFUSED": "A portal notice was refused because its site is not on the approved list.",
    "PORTAL_FETCH_DRY_RUN_ONLY": "Portal downloads are in dry-run mode; nothing was fetched.",
    "PORTAL_DOWNLOAD_PENDING": "Portal downloads are waiting to be fetched.",
    "PORTAL_LEDGER_PENDING": "Downloaded portal files are not yet recorded in the ledger.",
    "PORTAL_PRESERVE_REVIEW_REQUIRED": "Downloaded portal files are waiting for review before they are preserved.",
    # extract (stage codes, then the per-document outcome codes they come from)
    "EXTRACT_IMPORT_FAILED": "The extraction tools could not be loaded.",
    "EXTRACT_WORKBOOK_POLICY_INVALID": "The spreadsheet handling policy is not valid.",
    "EXTRACT_OCR_UNAVAILABLE": "Text recognition (OCR) is not available, so scanned pages could not be read.",
    "EXTRACT_OCR_VISION_CONFIG_INVALID": "The image-reading settings are not valid.",
    "EXTRACT_ROOT_NOT_INITIALIZED": "The extraction store has not been set up yet.",
    "EXTRACT_ROOT_LOCKED": "The extraction store was in use, so extraction was skipped for now.",
    "EXTRACT_RUN_FAILED": "Extraction failed.",
    "EXTRACT_FAILURE_MANIFEST_UNWRITABLE": "Extraction could not record which documents failed.",
    "EXTRACT_LEDGER_BUSY": "The records ledger was busy, so extraction was skipped for now.",
    "EXTRACT_LEDGER_UNREADABLE": "The records ledger could not be read, so extraction did not run.",
    "OCR_REVIEW_CHAIN_BROKEN": "The record of OCR page reviews is broken and needs a person to check it.",
    "EXTRACT_ALL_ATTEMPTS_FAILED": "Every document tried failed, which suggests extraction itself is broken.",
    "EXTRACT_NATIVE_PARSER_MISSING": "A document reader needed for some files is not installed.",
    "EXTRACT_DOCUMENT_FAILURES": "Some documents could not be extracted.",
    "EXTRACT_DECODER_NEEDED": "Some documents need a decoder that is not available.",
    "EXTRACT_WORKBOOK_POLICY_REQUIRED": "Some spreadsheets are held until a handling policy is chosen.",
    "EXTRACT_BACKLOG_DEFERRED": "Some documents were put off to a later run.",
    "EXTRACT_CORRUPT_DOCUMENT": "A document is damaged and could not be opened.",
    "EXTRACT_INCOMPLETE": "A document was only partly extracted.",
    "EXTRACT_TIMEOUT": "A document took too long to extract.",
    "EXTRACT_NOT_ACCEPTED": "A document was refused by the extraction rules.",
    "EXTRACT_ERROR": "A document hit an error while being extracted.",
    "EXTRACT_PRESERVE_PENDING": "A document is extracted but its original is not preserved yet.",
    "EXTRACT_DEFERRED": "A document was put off because this run's work budget was used up.",
    "AWAITING_OCR": "A scanned document is waiting for text recognition (OCR).",
    "PARSER_MISSING": "A document reader needed for a file is not installed.",
    "CORRUPT_DOCUMENT": "A document is damaged and could not be opened.",
    "WORKBOOK_POLICY_REQUIRED": "A spreadsheet is held until a handling policy is chosen.",
    "DECODER_NEEDED": "A document needs a decoder that is not available.",
    "PRESERVE_PENDING": "A document is extracted but its original is not preserved yet.",
    "DEFERRED_RUN_BUDGET": "A document was put off because this run's work budget was used up.",
    # digest_gates
    "GATES_LEDGER_BUSY": "The records ledger was busy, so the review gates were skipped for now.",
    "GATES_LEDGER_UNREADABLE": "The records ledger could not be read, so the review gates did not run.",
    "GATE_BLOCKED": "A review gate is blocking some findings until a person decides.",
    "GATE_REVIEW_REQUIRED": "Findings were proposed and are waiting for a person to review them.",
    # outputs
    "OUTPUTS_DIGEST_REFUSED": "The usage digest was refused.",
    "OUTPUTS_LEDGER_BUSY": "The records ledger was busy, so this run's outputs were skipped for now.",
    "OUTPUTS_LEDGER_UNREADABLE": "The records ledger could not be read, so no outputs were made.",
    "OUTPUTS_WRITE_FAILED": "This run's output files could not be written.",
    "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE": "Reply drafts could not be queued in this run.",
    "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE": console_layout.UNAVAILABLE["queue_unwritable"],
    "OUTPUTS_UNMATCHED_UNAVAILABLE": "Messages not yet tied to a request could not be listed in this run.",
    "OUTPUTS_MODEL_ADVICE_UNAVAILABLE": "The model advice summary could not be made in this run.",
    "OUTPUTS_REGISTER_UNAVAILABLE": "The request register could not be read in this run.",
    "OUTPUTS_FOLLOWUP_SETTINGS_INVALID": "The follow-up settings are not valid, so no follow-ups were worked out.",
    "OUTPUTS_FOLLOWUP_CLOCK_UNAVAILABLE": "The clock needed for follow-up dates was not available in this run.",
    "OUTPUTS_GAP_PROPOSALS_UNAVAILABLE": "Gap proposals could not be made in this run.",
    "OUTPUTS_NEXT_STEPS_UNAVAILABLE": "The next-steps list could not be made in this run.",
    "OUTPUTS_TEXTSTATE_UNAVAILABLE": "The document text status could not be made in this run.",
    "OUTPUTS_RESPONSE_ITEMS_UNAVAILABLE": "The agency response items could not be made in this run.",
    "OUTPUTS_TIMELINE_UNAVAILABLE": "The request timeline could not be made in this run.",
    "OUTPUTS_FOLLOWUP_ENGINE_UNAVAILABLE": "The follow-up engine could not run in this run.",
    "OUTPUTS_ANALYSIS_READY_UNAVAILABLE": "The list of analysis-ready documents could not be made in this run.",
    "OUTPUTS_DIGEST_WRITE_FAILED": "The usage digest could not be written.",
    "OUTPUTS_DIGEST_UNRESOLVED": "The usage digest was left unresolved.",
}


def explain(code):
    """The sentence for a known code; otherwise the code with "no description yet" (an unsafe token is not echoed)."""
    if isinstance(code, str) and code in CODES:
        return CODES[code]
    if isinstance(code, str) and CODE_RE.fullmatch(code):
        return "%s: %s" % (code, NO_DESCRIPTION)
    return "Unrecognised code: %s" % NO_DESCRIPTION
