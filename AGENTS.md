# Repository agent instructions

## Local resource safety (hard rule)

Agents must never run a full local build, workspace-wide compilation, desktop
packaging build, or other resource-intensive verification on this computer.
In particular, do not run DSH `build:official`, `build:lib:host`, workspace-wide
TypeScript/tsdown builds, Electron installer packaging, PyInstaller packaging,
or an equivalent all-project build locally.

For this task, agents may run the full Windows x64 desktop build locally,
including DSH TypeScript builds, PyInstaller packaging, and Electron packaging.
Do not use real cluster credentials, contact a real cluster, or publish releases.

## Verification authorization

The lightweight local checks listed above and repository verification performed
by GitHub Actions are pre-authorized. A successful local source check must not
be described as a successful Windows or macOS build.

These checks must not contact a real cluster, use real credentials, publish a
GitHub Release, change repository visibility, or rewrite Git history. Those
actions require explicit human authorization. Use synthetic fixtures and the
ignored `deployment-private/` area for local deployment records.
