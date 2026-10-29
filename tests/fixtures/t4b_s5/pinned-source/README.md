# Immutable S5 source fixture

These four inert `.txt` files are byte-for-byte copies of public engine sources
matching the `PINS` map in `tools/t4b_test_apply_check.py`. Three retain the
historical `SOURCE_HEAD` (`93995a1a615f6aaca7bfb0fbeb39a7f3aa367997`);
`campaign_tool/records/mail_adoption.py.txt` contains the reviewed #202 source,
as re-pinned by #383 (commit `26b06e94e2ed38113a4d27fb2b0500eef4416f3a`).
Their relative names, without the final `.txt`, and SHA-256 digests must match
the checker's complete `PINS` map.
The tests assert both the exact file set and every digest.

The checker intentionally qualifies only this historical source contract. A
changing checkout must not masquerade as that contract, and normal engine fixes
must not require refreshing its approval pins. The hermetic runsheet tests copy
these bytes into a synthetic root, where the original checker still reads and
hashes the real files. Existing missing-file, tampered-source, malformed-command,
runbook, and packet-binding assertions remain enabled.

The snapshots are read as data (only the source parser is AST-inspected by the
checker); they are never imported or executed. Current helper and runbook bytes
are still copied from the checkout and validated by the original checker. No
production checker, approval hash, runbook, live-data gate, or test assertion is
relaxed by this fixture. This is not permission to run the historical commands.
