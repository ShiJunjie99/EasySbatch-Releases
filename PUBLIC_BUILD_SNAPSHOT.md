# Beta EasySbatch public build snapshot

This orphan branch is a history-free snapshot of private development commit
`8ab439d`. It exists only so GitHub's native Windows x64 and Apple Silicon
macOS runners can produce unsigned Beta EasySbatch test artifacts.

The current Beta scans a selected local workspace, validates `JobSpec`, and
renders an sbatch script preview. It cannot connect to SSH or Slurm, execute a
job, submit a job, or monitor a job. Do not use it for production workloads.

No license for the EasySbatch project is granted by this snapshot. The pinned
DeepSeek Harness dependency retains its own upstream MIT license. No previous
EasySbatch Git history or private deployment configuration is included here.
