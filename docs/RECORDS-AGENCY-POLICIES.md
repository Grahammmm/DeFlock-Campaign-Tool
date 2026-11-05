# Agency policy register (`agency-policies.csv`)

One row = one section of one agency's own written policy, so drafts can cite it ("Policy 466.5(e)") and
policy-versus-practice gaps can be found by code. The register is data; no agency or campaign is named in code.

Header (exact): `agency,policy_id,section,quote,doc_sha256,page,adopted_date`

- `agency`: lowercase slug. `policy_id`, `section`: required text. `quote`: short passage, at most 400 characters.
- `doc_sha256`: 64 lowercase hex characters of the source document. `page`: whole number, 1 or more.
- `adopted_date`: `YYYY-MM-DD`, not in the future, or blank when the agency has not produced it.
- A row repeating agency + policy + section + hash + page is reported as a duplicate.

Check a file (read only; exit 0 clean, 1 with problems named by line and field):

    records agency-policies path/to/agency-policies.csv [--json]

Python: `agency_policies.load(path)` returns `(rows, problems)`; `sections`, `find` and `cite` look rows up.
Nothing is sent, posted or published. Filling in real rows is REC-DATA-2.
