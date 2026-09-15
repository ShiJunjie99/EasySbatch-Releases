# Repository agent instructions

## Verification authorization

Running the repository's automated verification is pre-authorized. Agents may
run the full pytest suite, targeted tests, static checks, secret/private-data
scans, compile checks, PyInstaller packaging smoke, checksum verification, and
other local build validation without asking the user for approval each time.

These checks must not contact a real cluster, use real credentials, publish a
GitHub Release, change repository visibility, or rewrite Git history. Those
actions require explicit human authorization. Use synthetic fixtures and the
ignored `deployment-private/` area for local deployment records.
