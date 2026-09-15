# Local UI assets

No CDN, package manager, fonts or Node runtime is required by the Web app.
Only compiled assets actually used are included. The files are unmodified.

| Package | Version | License | Asset | Bytes |
| --- | --- | --- | --- | ---: |
| `@tabler/core` | 1.5.0 | MIT | `tabler-1.5.0/tabler.min.css` | 691465 |
| `htmx.org` | 2.0.10 | 0BSD | `htmx-2.0.10/htmx.min.js` | 51238 |

Retrieved 2026-09-08 from official npm distributions:

- https://registry.npmjs.org/@tabler/core/-/core-1.5.0.tgz
- https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.10.tgz

Tabler's CSS includes its copyright header. The npm package omits the root
license; `tabler-1.5.0/LICENSE` is the official MIT text retrieved from
https://raw.githubusercontent.com/tabler/tabler/dev/LICENSE (2018–2026 copyright).
HTMX's license is taken directly from its package. No unrelated bundled libraries
or their licenses/assets are included. Tabler JS is unnecessary: disclosures use
native HTML details, navigation uses ordinary links, and requests use HTMX.

SHA-256:

```text
tabler.min.css  4cdeade29286540dff94acfeb6ea9ea6a16bad4a64ff5604f659414b7c954cd5
Tabler LICENSE 4f88a82d13be3c5c63a12c5631eae914aa4381b6dc17641bf1ab85f3f8f6c8a5
htmx.min.js    71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de
HTMX LICENSE  d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38
```

Update intentionally: verify official version/license/integrity, replace only
these assets, update template URLs and packaging metadata, then run offline
pytest and the browser/CSP checks. No runtime update checks. Source maps omitted.
Upstream HTMX contains optional eval-dependent functionality; this app disables it
with `allowEval: false` and CSP (no `unsafe-eval`), and disables script fragments,
inline indicator styles and history storage. Our own JS uses no eval/Function.
