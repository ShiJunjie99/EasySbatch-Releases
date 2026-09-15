# Beta EasySbatch public build snapshot

This branch is the public build snapshot corresponding to private development
commit `ab0d141`. It exists so GitHub's native Windows x64 and Apple Silicon
macOS runners can produce unsigned Beta EasySbatch test artifacts without a
local full build.

The current Beta scans a selected local workspace, validates `JobSpec`, reads
aggregate Slurm capacity, recommends a registered resource shape, renders and
saves an immutable task draft, and shows task history. A human can submit the
exact reviewed draft from the task panel and manually refresh its status.
Cluster access is restricted to fixed Slurm commands over the operating
system's OpenSSH client; the application does not read or store SSH passwords
or private keys. The owner-approved `10.158.132.77` preset includes only shared
environment loading steps; personal environments remain excluded. Other hosts
receive a no-command default environment while compute capacity is read live
from Slurm. Do not use unsigned Beta artifacts for production workloads.

No license for the EasySbatch project is granted by this snapshot. The pinned
DeepSeek Harness dependency retains its own upstream MIT license. No previous
EasySbatch Git history or private deployment configuration is included here.
