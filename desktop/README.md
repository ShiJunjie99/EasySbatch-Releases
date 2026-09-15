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
- read the automatically selected server software/environment catalog;
- compare visible eligible partitions and recommend a registered resource shape;
- validate a strict `JobSpec`;
- render an sbatch script preview from the automatically selected profile set
  and issue a digest tied to that exact preview;
- save a resolved job as a local, reviewable task draft.

The model cannot submit a job. Submission is available only in the **Task
history** panel after the user opens the exact saved script and confirms the
specific record. The product panels also provide manual task creation, complete
task detail, cluster overview, bounded remote-directory selection, and manual
status refresh. They do not expose Shell, arbitrary filesystem mutation, Web
search, skills, todos, goals, workflows, or subagents. DSH telemetry,
third-party desktop plugins, and automatic updates are disabled.

Cluster access uses the operating system's OpenSSH executable in batch mode.
Only the fixed `sinfo`, `squeue`, `scontrol`, `sacct`, and `sbatch --parsable`
forms already used by the Web version are allowed for job operations. The
user-driven task form has a separate fixed, read-only directory-listing call;
it returns at most 500 names and metadata items, never file contents, and is
not available to the model. The application never reads
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

Saving the connection also creates the product-owned `harness/profiles.yaml`
and `harness/server-catalog.yaml`.
For `10.158.132.77`, Beta selects the previously audited cluster-wide shared
environments. Personal paths and environments are intentionally excluded. Its
catalog contains shared GROMACS, TOPS, SCFT, and toolchain facts with their
original verification status. For any other host, Beta creates a no-command
`cluster-default` environment and an empty software catalog instead of guessing
modules, Conda paths, or installed software. In both cases,
partitions, nodes, CPU/GPU capacity, and queue state are read live from Slurm;
they are not copied from a static template. Task history is stored separately
in `harness/jobs.sqlite3`, and immutable submission scripts are staged below
`harness/runs/`.

Expected checks:

1. The title/sidebar say **Beta EasySbatch**, the start page has no DeepSeek
   fish branding, and no browser opens.
2. The left navigation is compact, the chat is narrower, and the **Project
   files** window opens on the right. Drag its blue divider to resize it.
3. The left navigation contains **New task**, **Task history**, and **Cluster
   resources**. New task supports software/environment selection, explicit or
   cluster-default memory and walltime, live resource recommendation, remote
   directory selection, script preview, and local draft saving.
4. With no `cluster.json`, the UI says the cluster is not configured and keeps
   the submit button disabled. Saving a valid connection automatically selects
   the known shared preset or the safe `cluster-default` fallback.
5. Project scanning and the Project files window cannot choose a path outside
   the selected workspace.
6. Asking the model to run Shell, edit a file, browse the Web, or submit a job
   produces no matching tool call.
7. A valid JobSpec can be validated, recommended from a fresh cluster snapshot,
   rendered, and saved as a draft. Saving requires the digest returned for the
   unchanged preview; editing any field invalidates it. The exact script and
   complete JobSpec/evidence appear in Task history.
8. Submission requires a record-specific confirmation dialog. A submitted task
   shows its Slurm job ID, manual status refresh, and declared/resolved log
   paths. As in the Web version, Beta does not read log contents.

## Updating DSH

Change the tag, commit, URL, and checksum together in `upstream.lock.json`, then
run preparation. Every patch must still apply without fuzz. Review DSH's
security and breaking-change notes before accepting the update, because DSH is
currently a developer preview and its standard presets intentionally execute
model-generated commands; Beta EasySbatch never ships those presets.
