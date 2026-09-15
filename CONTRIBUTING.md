# Contributing

Install the development dependencies and run the full test suite:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test,web]'
.venv/bin/python -m pytest -q
```

Keep these boundaries intact: the Analyzer and Harness remain server-side;
the local provider owns only the user's provider call; SSH credentials and AI
keys never enter tests, logs, fixtures, or command arguments. Use synthetic
`alice`, `bob`, `cluster.example.edu`, and temporary directories. Do not run
real cluster smoke tests in pull-request CI or add real catalogs, scientific
inputs, screenshots, databases, or generated logs.
