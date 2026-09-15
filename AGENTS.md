# Repository agent instructions

## Local resource safety (hard rule)

Agents must never run a full local build, workspace-wide compilation, desktop
packaging build, or other resource-intensive verification on this computer.
In particular, do not run DSH `build:official`, `build:lib:host`, workspace-wide
TypeScript/tsdown builds, Electron installer packaging, PyInstaller packaging,
or an equivalent all-project build locally. Windows and macOS desktop builds
must run in GitHub Actions, not on the user's computer.

Local verification is limited to lightweight, narrowly targeted checks such as
source inspection, `git apply --check`, individual low-cost tests, small syntax
checks, secret/private-data scans, and checksum verification. If a check begins
using substantial CPU or memory, stop it immediately. Do not widen a targeted
check into a full suite or build without moving it to GitHub Actions.

## Verification authorization

The lightweight local checks listed above and repository verification performed
by GitHub Actions are pre-authorized. A successful local source check must not
be described as a successful Windows or macOS build.

These checks must not contact a real cluster, use real credentials, publish a
GitHub Release, change repository visibility, or rewrite Git history. Those
actions require explicit human authorization. Use synthetic fixtures and the
ignored `deployment-private/` area for local deployment records.
