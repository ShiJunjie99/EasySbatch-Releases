# EasySbatch security model

EasySbatch uses SSH as the cluster authentication boundary. Each authenticated
session starts a Worker under the remote Linux user, checks the kernel-bound
identity, and binds the WebSession to that exact Worker and Launcher session.
Slurm operations therefore inherit the real user identity.

Project context sent to an AI provider is bounded and treated as untrusted
input. The server-side `AIProjectAnalyzer` constructs the request. Provider
responses return to the server for strict parsing, schema validation, evidence
and provenance checks, Harness validation, and final user review. The model has
no arbitrary shell execution route.

In `local_user_provider` mode the user's Launcher calls the fixed DeepSeek
HTTPS endpoint. The API key is read from the local OS credential store (or an
explicit memory-only session fallback) and never crosses SSH, enters the
Worker/Broker/WebSession, or is written to the server. Users provide and pay
for their own provider account.

HPC host, SSH port, username, known-hosts path, catalog, database and workspace
are deployment configuration. They are not shared defaults or credentials.
