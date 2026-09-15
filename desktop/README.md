# Beta EasySbatch Desktop

This directory is the maintainable desktop product layer over the upstream
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). The DSH
source is not vendored: `upstream.lock.json` pins an exact tag, commit, archive
URL, and SHA-256 checksum. `dsh-overlay/` adds the EasySbatch bundle, restricted
tool adapter, and single safe Agent preset; `dsh-patches/` contains the small
native-shell integration delta.

## Product boundary

The application is a native Electron package and does not open a browser or
listen on localhost. DSH owns chat, sessions, model settings, and the desktop
window. The frozen Python sidecar reuses EasySbatch's strict `JobSpec`, bounded
project scanner, static profiles, and deterministic sbatch renderer.

The desktop Beta exposes exactly these model tools:

- report capabilities;
- scan the currently selected workspace read-only;
- validate a strict `JobSpec`;
- render an sbatch script preview from a maintainer-supplied profile file.

It does not expose Shell, arbitrary filesystem access, Web search, skills,
todos, goals, workflows, subagents, SSH, Slurm, execution, or submission. DSH
telemetry, third-party desktop plugins, and automatic updates are disabled.

Model credentials currently use DSH's local credential store inside this
product's isolated user-data directory. The legacy launcher's operating-system
keychain integration has not yet been ported. Treat this as a Beta limitation:
use a restricted model API key, protect the local account, and do not distribute
production credentials with test packages.

## Supported packages

- Windows x64 (`win-x64`)
- macOS Apple Silicon (`mac-arm64`)

Intel macOS is intentionally not part of this product matrix.

## Prepare and verify on Linux

```bash
.venv/bin/python scripts/prepare_beta_desktop.py
cd build/beta-easysbatch/dsh
pnpm install --no-frozen-lockfile
pnpm run build:lib:host
```

Preparation downloads only the pinned DSH archive and rejects a checksum
mismatch. Pass `--source /absolute/path/to/deepseek-harness` for a network-free
build; the checkout must be clean and its `HEAD` must equal the pinned commit.

## Build installers

Install the repository and PyInstaller first, and use pnpm 11.7.0 with Node 26.
Each target must be built natively on the matching operating system:

```powershell
python -m pip install ".[test]" "pyinstaller==6.16.0"
python scripts/build_beta_desktop.py --target win-x64
```

```bash
python -m pip install '.[test]' 'pyinstaller==6.16.0'
python scripts/build_beta_desktop.py --target mac-arm64
```

Unsigned test artifacts are written below `dist/beta-easysbatch/<target>/`.
Use `--signed` only in the controlled release environment with the upstream
Windows or Apple signing variables configured. The GitHub workflow builds and
uploads unsigned test artifacts, but never publishes a GitHub Release.

## Manual test configuration

On first start, select a local project directory. The app stores its own DSH
state under the platform-specific Electron user-data directory, isolated from
a normal DSH installation. To render rather than only validate a JobSpec,
create `harness/profiles.yaml` below that Beta EasySbatch user-data directory,
using the existing `StaticProfiles` schema. A cluster administrator must audit
this file; public example profiles are not production configuration.

The expected profile locations are `%APPDATA%\Beta EasySbatch\harness\profiles.yaml`
on Windows and `~/Library/Application Support/Beta EasySbatch/harness/profiles.yaml`
on macOS. `examples/profiles.yaml` is suitable only as synthetic test input.

Expected checks:

1. The title/sidebar say **Beta EasySbatch** and no browser opens.
2. `easysbatch_capabilities` reports `submission_enabled: false`.
3. Project scanning cannot choose a path other than the selected workspace.
4. Asking the model to run Shell, edit a file, browse the Web, or submit a job
   produces no matching tool call.
5. A valid JobSpec can be validated; rendering requires the fixed profile file
   and returns text for review only.

## Updating DSH

Change the tag, commit, URL, and checksum together in `upstream.lock.json`, then
run preparation. Every patch must still apply without fuzz. Review DSH's
security and breaking-change notes before accepting the update, because DSH is
currently a developer preview and its standard presets intentionally execute
model-generated commands; Beta EasySbatch never ships those presets.
