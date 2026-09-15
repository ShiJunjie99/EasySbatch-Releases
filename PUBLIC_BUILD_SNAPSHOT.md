# Beta EasySbatch public build snapshot

This branch is the public build snapshot corresponding to private development
commit `5f203d8`. It exists so GitHub's native Windows x64 and Apple Silicon
macOS runners can produce unsigned Beta EasySbatch test artifacts without a
local full build.

The current Beta validates `JobSpec`, reads aggregate Slurm capacity, compares
eligible partitions, renders exact script previews, and shows full task detail.
The manual task panel can select registered software and environments, browse a
user-selected remote directory, perform a bounded read-only scan of its relevant
text, and apply memory or walltime only when an exact inspectable rule or project
declaration supports the value. AI preparations are persistent and revisioned:
unresolved drafts remain `NEEDS_INPUT`, resolved drafts become `READY_TO_SAVE`,
and only a human can save them into Task history after a fresh remote fingerprint
check. A second record-specific confirmation is required for Slurm submission.
Cluster access is restricted to fixed Slurm commands and fixed bounded read-only
directory operations over the application's Paramiko client. Users enter the
server IP, port, Linux username, and password inside the application and approve
the SSH host-key fingerprint on first use. A successfully verified password is
bound to that endpoint/account in Windows Credential Manager or macOS Keychain;
it is not written to configuration, SQLite, logs, arguments, or environment
variables. The application does not upload project files or store private keys.
The owner-approved `10.158.132.77:3088` preset includes only shared environment
and software facts; personal environments remain excluded. Other hosts receive
a no-command default environment and an empty software catalog while compute
capacity is read live from Slurm. Signed formal releases require protected
certificate secrets that are not currently configured. Do not use unsigned Beta
artifacts for production workloads.

No license for the EasySbatch project is granted by this snapshot. The pinned
DeepSeek Harness dependency retains its own upstream MIT license. No previous
EasySbatch Git history or private deployment configuration is included here.
