Historical source fixture

mail_adoption.source.txt is the exact campaign_tool/records/mail_adoption.py
blob from commit 93995a1a615f6aaca7bfb0fbeb39a7f3aa367997 (the checker's
SOURCE_HEAD). It is inert input for runsheet parser/pin tests, never imported
or executed. The tests verify its bytes against the unchanged production PINS
before constructing the disposable runsheet repository. This keeps tests
independent of Git history (including shallow CI checkouts), while current
producer integration continues to exercise campaign_tool/records/mail_adoption.py.

Regenerate from a full clone with:
    git show 93995a1a615f6aaca7bfb0fbeb39a7f3aa367997:campaign_tool/records/mail_adoption.py > tests/fixtures/t4b_s5/mail_adoption.source.txt

Do not refresh the historical source pins to match current production code.
