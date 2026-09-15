# M10-B8 Public GitHub Release Readiness

## A. Goal

Separate generic EasySbatch source and synthetic examples from private cluster
deployment, credentials, scientific inputs, runtime state, and historical
internal evidence. No GitHub visibility change or public Release was made.

## B–E. Public and private boundary

The public tree now uses `cluster.example.edu:22`, synthetic `alice`/`bob`
examples, `ClusterProfile`, and `config/examples/`. Usernames and cluster
metadata are local configuration. Private catalogs, paths, known-hosts,
databases, runs and logs belong under ignored `deployment-private/` or outside
the repository. The current lab material was preserved there before the public
documentation/data split.

The B7 model remains user-owned: DeepSeek keys are configured on the Launcher,
stored in the local OS credential store or explicit session memory, and never
sent to the server or SSH channel.

## F–I. Audit

Current-source marker scan found no real API key, password, private key, token,
cookie, or certificate key. Matches in scanner code and synthetic regression
fixtures are marker strings only. Artifact scanning passes for the current
Linux candidate. The Git history contains historical internal deployment and
scientific-data files and marker fixtures; no history rewrite was performed.
The original internal docs and scientific inputs remain preserved in the local
ignored archive for deployment owners, so public publication requires a review
and approved history cleanup plan.

## J–K. Licensing and ownership

No `LICENSE` file is present. Copyright ownership and PI/organization release
permission are not confirmed. Third-party frontend notices are recorded, but a
complete dependency license inventory remains outstanding. These are release
blockers; no license was invented.

## L–N. CI, builds, artifacts

The Actions workflow is tag-gated for Releases, uses read-only permissions for
build jobs, runs on Ubuntu/Windows/macOS native runners, runs artifact smoke,
and generates `SHA256SUMS`. The release job alone requests `contents: write`.
The current Linux artifact builds and scans successfully. Candidate SHA-256 is
`e50272f0e0a5a5bd4399a91e06f61257b8b2199ee62997c1f8a3ef9c1f9c34ac`.
Native Windows and macOS builds from this post-B8 source were not run in this
environment.

## O–Q. Fresh clone and private regression

The full test suite is retained and was run after the public configuration
changes: `2062 passed, 2 warnings`. A clean-environment Linux packaging smoke
is available. A fresh-tree compile, PyInstaller Linux build, artifact scan, and
packaging/B7 tests passed (`34 passed`) without private configuration. A private
lab regression still requires the
deployment owner to run its preserved profile; the lab itself was not contacted.

## R–T. Result

Current result: **PUBLIC_RELEASE_BLOCKED**. The repository is not made public,
and no GitHub Release is created. Required blockers are: approved license and
copyright/PI permission; approved removal or history cleanup of internal
deployment/scientific data; complete dependency license review; and current
Windows/macOS native artifact plus fresh-clone/private-regression evidence.
