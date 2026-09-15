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
project scanner, static profiles, deterministic resource recommender, sbatch
renderer, immutable task store, and single-attempt Slurm lifecycle.

The desktop Beta exposes these model tools:

- report capabilities;
- scan the currently selected workspace read-only;
- read an aggregate cluster snapshot;
- recommend an eligible partition and registered resource shape;
- validate a strict `JobSpec`;
- render an sbatch script preview from a maintainer-supplied profile file;
- save a resolved job as a local, reviewable task draft.

The model cannot submit a job. Submission is available only in the **Task
history** panel after the user opens the exact saved script and confirms the
specific record. The product panel also provides cluster overview and manual
status refresh. It does not expose Shell, arbitrary filesystem mutation, Web
search, skills, todos, goals, workflows, or subagents. DSH telemetry,
third-party desktop plugins, and automatic updates are disabled.

Cluster access uses the operating system's OpenSSH executable in batch mode.
Only the fixed `sinfo`, `squeue`, `scontrol`, `sacct`, and `sbatch --parsable`
forms already used by the Web version are allowed. The application never reads
or stores a password or private key; host verification, keys, and ssh-agent
remain under OpenSSH. The reviewed script is sent to `sbatch` over standard
input, so a local application path is never interpreted as a remote path.

Model credentials currently use DSH's local credential store inside this
product's isolated user-data directory. The legacy launcher's operating-system
keychain integration has not yet been ported. Treat this as a Beta limitation:
use a restricted model API key, protect the local account, and do not distribute
production credentials with test packages.

## Supported packages

- Windows x64 (`win-x64`)
- macOS Apple Silicon (`mac-arm64`)

Intel macOS is intentionally not part of this product matrix.

## Build and verification policy

Do not compile or package the DSH workspace on a development computer. The
Windows x64 and Apple Silicon macOS builds run only in
`.github/workflows/build-beta-desktop.yml`, using Node 24.18.0, pnpm 11.7.0,
PyInstaller, and the native target runner. Local work is limited to Python unit
tests, source inspection, and checking that the product patch applies to the
pinned DSH archive. The build workflow uploads unsigned test artifacts and does
not publish a GitHub Release.

## Manual test configuration

On first start, select a local project directory. The app stores its own DSH
state under the platform-specific Electron user-data directory, isolated from
a normal DSH installation. The local directory is used for bounded analysis
and file preview; `JobSpec.project_dir` and `work_dir` remain explicit POSIX
paths on the cluster. Beta does not silently upload or assume that local files
already exist remotely.

Open **Cluster resources** and enter the cluster display name, host, SSH port,
and Linux username. This writes non-secret metadata to `harness/cluster.json`;
the in-app form never asks for a password or key. Before testing the connection,
run the operating-system `ssh` command once so the user can verify the host
fingerprint and make key/ssh-agent authentication available.

To render, recommend, or submit, create `harness/profiles.yaml` below the Beta
EasySbatch user-data directory using the existing `StaticProfiles` schema. A
cluster administrator must audit this file; public example profiles are only
synthetic test inputs. Task history is stored separately in
`harness/jobs.sqlite3`, and immutable submission scripts are staged below
`harness/runs/`.

The expected profile locations are `%APPDATA%\Beta EasySbatch\harness\profiles.yaml`
on Windows and `~/Library/Application Support/Beta EasySbatch/harness/profiles.yaml`
on macOS. `examples/profiles.yaml` is suitable only as synthetic test input.

Expected checks:

1. The title/sidebar say **Beta EasySbatch**, the start page has no DeepSeek
   fish branding, and no browser opens.
2. The left navigation is compact, the chat is narrower, and the **Project
   files** window opens on the right. Drag its blue divider to resize it.
3. The left navigation contains **Task history** and **Cluster resources**.
4. With no `cluster.json`, the UI says the cluster is not configured and keeps
   the submit button disabled.
5. Project scanning and the Project files window cannot choose a path outside
   the selected workspace.
6. Asking the model to run Shell, edit a file, browse the Web, or submit a job
   produces no matching tool call.
7. A valid JobSpec can be validated, recommended from a fresh cluster snapshot,
   rendered, and saved as a draft. The exact script appears in Task history.
8. Submission requires a record-specific confirmation dialog. A submitted task
   shows its Slurm job ID, manual status refresh, and declared/resolved log
   paths. As in the Web version, Beta does not read log contents.

## Updating DSH

Change the tag, commit, URL, and checksum together in `upstream.lock.json`, then
run preparation. Every patch must still apply without fuzz. Review DSH's
security and breaking-change notes before accepting the update, because DSH is
currently a developer preview and its standard presets intentionally execute
model-generated commands; Beta EasySbatch never ships those presets.
