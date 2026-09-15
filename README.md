# EasySbatch

> **New desktop direction:** `Beta EasySbatch` now has a native Windows x64 and
> Apple Silicon macOS architecture based on a pinned DeepSeek Harness desktop
> runtime. It does not open an external browser or listen on localhost. See
> [desktop/README.md](desktop/README.md) for its security boundary, build
> commands, supported targets, and manual test procedure. The existing launcher
> and Web architecture below remains available during migration.

EasySbatch is an AI-assisted Slurm workflow for HPC users. It scans a project,
builds a bounded analysis request, prepares a structured `JobSpec`, shows the
generated submission script for review, and then submits and monitors the job.
The model never receives an unrestricted shell endpoint and the user retains
the final submission decision.

## Download

Alpha launcher builds are attached to GitHub Releases when a maintainer creates
a version tag. The intended asset names are:

- `EasySbatch-Windows-x86_64.exe`
- `EasySbatch-macOS-arm64.dmg` or `.zip`
- `EasySbatch-Linux-x86_64`
- `SHA256SUMS`

The alpha builds are unsigned/not notarized. Do not disable operating-system
security controls; verify the checksum and obtain a newer release if a check
fails.

## Quick start

1. Download the launcher for the local operating system.
2. Run `EasySbatch --configure-cluster` and enter the cluster name, host, SSH
   port, username, and the service UID supplied by the cluster administrator.
3. Start EasySbatch and complete the normal OpenSSH authentication prompt.
4. Optionally run `EasySbatch ai configure` to save your own DeepSeek API key in
   the local OS credential store. AI is optional; manual Slurm workflows remain
   available without it.
5. Open the workspace, scan a project, review the structured draft, and submit
   only after confirmation.

The cluster profile contains only non-secret connection metadata. SSH passwords,
private keys, and AI keys are never command-line arguments or repository files.
The B7 local provider sends the user's prompt from that user's device to the
fixed DeepSeek provider. The key does not cross SSH or reach the server.

## Server installation

The launcher is not a hosted Slurm service. A cluster administrator installs
the EasySbatch server/Worker in a user-level Python environment on a Linux
login node with OpenSSH and Slurm available. Configure a private Cluster
Profile, Server Catalog, database, runs directory, and known-hosts file for
that deployment. The public examples in `config/examples/` are synthetic and
must be audited before use. No root access or shared API key is required.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[web]'
.venv/bin/python scripts/start_web.py --config /path/to/private-config.toml --ssh-first
```

The server-side Analyzer, schema validation, Harness, provenance checks, and
JobSpec drafting stay on the cluster. The per-user Worker runs with the
authenticated Linux identity, and Slurm inherits that identity.

## Security model

- SSH authenticates the user and the Worker verifies the kernel-bound identity.
- A WebSession is bound to its exact Worker and Launcher session; sessions are
  never selected only by username.
- The Harness validates provider output before any JobSpec can be submitted.
- No arbitrary model-generated shell is executed.
- In `local_user_provider` mode, the DeepSeek key is stored only in the user's
  Windows Credential Manager, macOS Keychain, Linux secure keyring, or explicit
  memory-only session fallback.
- EasySbatch is not a privilege-escalation service and provides no telemetry or
  automatic update service.

See [SECURITY_MODEL.md](SECURITY_MODEL.md) and [SECURITY.md](SECURITY.md).

## Development

```bash
.venv/bin/python -m pip install -e '.[test,web]'
.venv/bin/python -m pytest -q
```

Use only synthetic users and credentials in tests. The desktop Beta has one
explicit public preset for `10.158.132.77`; it contains cluster-wide shared
loading steps only. Keep user-specific paths and environments, full real
catalogs, databases, runs, logs, project inputs, and other deployment profiles
under the ignored `deployment-private/` directory or outside the repository.

The current alpha is intentionally incomplete. Windows/macOS native credential
tests and real two-user DeepSeek acceptance are still outstanding. Historical
M10 transport experiments remain development records; they are not the default
AI architecture. See [M10-B8 release readiness](docs/M10-B8_Public_GitHub_Release_Readiness交付说明.md).
