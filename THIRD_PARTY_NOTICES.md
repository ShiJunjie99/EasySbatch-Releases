# Third-party notices

The Web UI bundles Tabler Core 1.5.0 under MIT and HTMX 2.0.10 under 0BSD;
their license texts and source checksums are recorded in
`src/sbatch_agent/static/vendor/README.md`.

Python dependencies are installed from the version ranges in `pyproject.toml`.
A release must attach a generated dependency license inventory and review
redistribution terms for every bundled dependency, including keyring backends,
certifi, PyYAML, FastAPI, Jinja2, Uvicorn, httpx, Pydantic and their transitive
dependencies. This repository does not make a blanket license claim for that
inventory; the review remains a release gate.
