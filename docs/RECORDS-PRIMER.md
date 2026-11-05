# California records primer

This primer explains, in plain language, the six kinds of records the shipped `us-ca` law package asks for when
the kit drafts a California records request about automated license plate readers (ALPR: cameras and software that
turn license plate images into searchable data). It is orientation for organizers who are about to receive, or are
reading, an agency's response.

**This is not legal advice.** It does not tell you what an agency must produce, what it may withhold, or whether
anyone broke a law. The `us-ca` package in [jurisdictions/us-ca/package.json](../jurisdictions/us-ca/package.json)
is `status: draft` with no reviewers and no review date, so it has not been independently legally reviewed; a rule
marked `verified` inside it does not promote the whole package. See [LAW-PACKAGES.md](LAW-PACKAGES.md) for what
the statuses mean. Questions about rights or remedies belong with a qualified human.

### How to read each section

Each section below is named by the package's `scope_id` and title and has the same five parts. The "items" a scope
lists are what this tool **asks for**; they are not evidence that an agency holds such a record. Suggestions about
who may hold a record, and what a production may look like, are possible offices to ask the custodian to check and
hypothetical examples. The package does not say how often any file format or custody arrangement occurs, so this
primer does not either. Statements about what the law requires are tied to the named package rule, whose statutory
citation is in [the package](../jurisdictions/us-ca/package.json).

### Four checks for every response

1. **Dates covered.** Compare the period the records cover with the period you asked for.
2. **Every asked part.** Go through your request item by item and note which parts were answered, which were
   withheld and which were not mentioned at all.
3. **A readable export.** Open every attachment and export. Note truncated, empty or unreadable files.
4. **Redactions explained.** If something is withheld or blacked out, ask the agency for its stated basis.

These are organizer actions, not a legal entitlement. A response of "no records" or a partial response is not proof
that records do not exist, that anyone acted wrongly, or that a search was unlawful. Keep the originals privately and
do not publish query data, identities or unreviewed records; see [DATA-HANDLING.md](DATA-HANDLING.md). For choosing
agencies and verifying a custodian before you send, see [AGENCY-DISCOVERY.md](AGENCY-DISCOVERY.md).

## agreements — ALPR vendor agreements and funding

### What these records are

The tool asks for the contracts and related papers behind an ALPR system: executed agreements, purchase orders,
statements of work, amendments, renewals and exhibits; terms of service and data-use or license agreements; grant,
funding, invoice and budget records; and records showing how many fixed or mobile cameras are deployed under each
agreement. The package ties this scope to the definitions rule
[`ca-civ-1798.90.5-definitions`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.5, which defines an ALPR
system, operator and end-user) and to the sharing-limits rule
[`ca-civ-1798.90.55-sharing-limits`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.55(b), under which a
public agency shall not sell, share or transfer ALPR information except to another public agency, as otherwise
permitted by law; data hosting and towing services are not counted as sharing).

### Where to ask

Ask the agency's records custodian, and ask them to check procurement and finance staff. Where the purchase or grant
needed approval outside the agency, a city or county clerk or purchasing office may hold the approved contract. A
vendor may host a system, but a records request goes to the agency, which decides whether a record it controls
sits elsewhere.

### What a production may look like

Hypothetical examples: a contract PDF with amendments and exhibits attached, a funding or invoice spreadsheet, and a
list or map of camera locations.

### What to check

- The parties, signatures and the effective term; whether renewals and every amendment and exhibit are included.
- The funding period and amounts against the invoices and grants you asked about.
- Whether the camera number and location question was answered, or answered separately from the contract.
- Use the four shared checks above: dates, every part, readable files, explained redactions.

### Where practice varies

Which office keeps contracts, whether a vendor's terms are kept as separate attachments, what dates are retained and
what is redacted vary by agency and must be confirmed for each request. Whether a contract term amounts to
"sharing" under § 1798.90.55(b) is a legal question this primer does not answer.

## policies — Usage and privacy policies, retention, and training

### What these records are

The tool asks for the current ALPR usage and privacy policy and each prior version with adoption dates; records
showing where and when it was made public and posted on the agency website; retention and destruction schedules;
access-control procedures naming the job titles allowed to use the system; and training materials and requirements.
The package rules behind this are
[`ca-civ-1798.90.51-usage-and-privacy-policy`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.51(b): an
ALPR operator must implement a usage and privacy policy, make it publicly available in writing and, if it has a
website, conspicuous online; the minimum contents include authorized purposes, job titles and training
requirements, and the retention length and process for deciding whether to destroy data),
[`ca-civ-1798.90.53-end-user-usage-and-privacy-policy`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.53,
the corresponding duty for an ALPR end-user, subject to the persons excluded by § 1798.90.5(a)) and
[`ca-civ-1798.90.51-security-procedures`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.51(a): reasonable
operational, administrative, technical and physical safeguards).

### Where to ask

Ask the agency's records custodian, who may route the request to whoever keeps policies and training. Records that
show how the system is actually set up (for example a retention setting) may be kept in the agency's administrator
account on a vendor system, so ask the custodian to check there as well.

### What a production may look like

Hypothetical examples: a policy PDF, several dated versions of it, training slide decks or sign-in sheets, and
screenshots of retention or access settings.

### What to check

- The date each policy version was adopted, and whether the date of any settings screenshot matches the policy date.
- Whether the retention rule is complete: how long, and the process for deciding when to destroy.
- Whether the authorized job titles, training and web-posting questions each received an answer.
- Use the four shared checks above.

### Where practice varies

Who keeps these papers, whether old versions are retained, whether settings can be exported and what is redacted vary
and must be confirmed for each request. Whether a policy meets the minimum contents in § 1798.90.51(b) is for a
qualified human to decide; this primer does not.

## sharing — Data-sharing authorizations and recipients

### What these records are

The tool asks for records identifying every agency or entity with which ALPR information is shared or that has been
granted access (including through vendor network or national lookup features); sharing authorizations,
memoranda of understanding and settings history with the date of each change; any sharing with, or access by,
out-of-state or federal agencies or private entities, and any review done in response to California DOJ Information
Bulletin 2023-DLE-06; and requests received from other agencies and the responses. The package rules are
[`ca-civ-1798.90.55-sharing-limits`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.55(b), above),
[`ca-oag-2023-dle-06-out-of-state-sharing-guidance`](../jurisdictions/us-ca/package.json) (the Attorney General's
bulletin, which the package describes as interpretive guidance and not a statute or regulation, reading
§ 1798.90.55(b) to bar sharing with private entities and out-of-state or federal agencies; the package marks it
`likely`) and
[`ca-gov-7284.6-values-act-immigration-enforcement-limits`](../jurisdictions/us-ca/package.json) (Gov. Code § 7284.6,
the California Values Act; the package says applying it to ALPR data is interpretive, not an established duty, and
marks it `needs_attorney_review`). The bulletin is published by the California Department of Justice at
<https://oag.ca.gov/system/files/media/2023-dle-06.pdf>.

### Where to ask

Ask the agency's records custodian. The list of who has access, and the history of sharing settings, may sit in an
administrator account on a vendor system, so ask the custodian to check there; authorizations and memoranda may be
kept as separate files, and a city or county clerk may hold approved agreements.

### What a production may look like

Hypothetical examples: a CSV or spreadsheet of recipient agencies, authorization or memorandum PDFs, and dated
screenshots of sharing settings.

### What to check

- The dates covered, and whether a screenshot shows only the current setting rather than its history.
- That an enabled setting is not read as proof that anyone actually accessed data, and a recipient list is not read
  as proof of a violation.
- Whether out-of-state, federal and private access, the bulletin review and incoming requests each received an answer.
- Use the four shared checks above.

### Where practice varies

How sharing is configured, whether history is kept and what is redacted vary and must be confirmed for each request.
Whether a given recipient or arrangement is lawful is a question for a qualified human.

## audit-logs — Access and search logs

### What these records are

The tool asks for the access records an ALPR operator keeps under
[`ca-civ-1798.90.52-access-records`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.52): when the operator
accesses or provides access to ALPR information it must record, at a minimum, the date and time, the license plate
number or other data used to query, the username and, as applicable, the person's organization, and the purpose. The
tool also asks for field definitions or a data dictionary, and for existing aggregate counts of searches and hotlist
alerts. It asks for these in native electronic format for the stated date range.

### Where to ask

Ask the agency's records custodian and the agency's system administrator. Search logs are often kept in the vendor's
system, but the request goes to the agency, not the vendor; do not contact or log in to a vendor system yourself.

### What a production may look like

Hypothetical examples: a CSV or spreadsheet export, with a separate sheet or document defining the columns, and a
summary report of search counts.

### What to check

- The date range in the export against the range you asked for, and the time zone and meaning of each column.
- That the rows open and are readable, and whether the export looks cut off by page limits or row caps.
- That the purpose and affiliation columns are present, and which fields were withheld.
- Use the four shared checks above. Do not publish query terms, usernames or identities from a log.

### Where practice varies

What the system can export, which fields are kept, how long logs are retained and what is withheld vary and must be
confirmed for each request. Whether a log shows compliance with § 1798.90.52 is for a qualified human to decide.

## compliance — Audits and corrective actions

### What these records are

The tool asks for completed internal or external audits of ALPR use, security or privacy compliance (including the
periodic system audits an end-user policy describes); documented corrective actions, disciplinary referrals or
misuse findings; records of security incidents or unauthorized access; and data-accuracy reviews and error
corrections. The package rules are
[`ca-civ-1798.90.51-security-procedures`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.51(a)),
[`ca-civ-1798.90.52-access-records`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.52) and
[`ca-civ-1798.90.53-end-user-usage-and-privacy-policy`](../jurisdictions/us-ca/package.json) (Civ. Code § 1798.90.53,
whose minimum contents include a periodic system-audit process and accuracy and error-correction measures).

### Where to ask

Ask the agency's records custodian, who may route the request to whoever keeps audit, oversight, security and
system-administration records.

### What a production may look like

Hypothetical examples: an audit report PDF with attachments, a table of corrective actions, and an incident
summary.

### What to check

- The period and the system that each audit covers, and whether its attachments are included.
- Whether each requested category (audits, corrective actions, incidents, accuracy reviews) was addressed.
- The difference between an audit finding or recommendation and an action actually completed.
- Use the four shared checks above. Do not assume personnel or security details will be released; ask what basis the
  agency gives if they are withheld.

### Where practice varies

Which office does audits, whether reports exist, and what is redacted vary and must be confirmed for each request.
The absence of an audit is not proof that none was required or done.

## hearing-records — Public-meeting records before implementation

### What these records are

Civ. Code § 1798.90.55(a), recorded in the package as
[`ca-civ-1798.90.55-public-comment-before-implementation`](../jurisdictions/us-ca/package.json), says a public agency
that operates or intends to operate an ALPR system must, before implementing the program, provide an opportunity
for public comment at a regularly scheduled public meeting of its governing body. The tool asks for the agenda,
staff reports, minutes and recording of the meeting at which the program was considered; public comments received and
any written replies; and records showing when the program was first implemented or expanded.

### Where to ask

Ask the clerk of the governing body (the city or county clerk or an equivalent records office) for meeting records,
and ask the agency's records custodian for the implementation or expansion dates.

### What a production may look like

Hypothetical examples: an agenda packet PDF, minutes, a link or file for the recording, and a document or email
stating an implementation date.

### What to check

- The governing body, meeting date and agenda item, and that staff reports and comments are attached.
- That the recording opens and plays, and that the implementation-date part of the request was answered.
- Use the four shared checks above. A procurement vote, missing minutes or a later expansion does not by itself show
  that § 1798.90.55(a) was met or missed.

### Where practice varies

Which body keeps meeting records, how long recordings are retained, and whether comments were kept vary and must be
confirmed for each request. Whether public comment was properly provided is for a qualified human to decide.
