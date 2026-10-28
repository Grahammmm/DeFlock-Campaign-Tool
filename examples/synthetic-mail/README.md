# Synthetic mail fixtures

Fictional agency replies used by `tests/runner/test_classify_mail.py`. Addresses
are `example.invalid`; the bodies contain no real names, plates, addresses or
case numbers. The production message with PDF and workbook attachments is built
inside the test so no binary fixture is committed. The files carry an `.eml.txt` suffix because the repository scanners forbid committing `.eml`, `.pdf`, `.xlsx` and `.zip` files; that safeguard is deliberate. These files are
synthetic test input, not campaign correspondence.
