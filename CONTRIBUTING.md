# Contributing

Issues are welcome: bugs, a retailer page that changed, questions about the design.

The public repository is a periodically regenerated single-commit snapshot of a privately developed
tree, so a pull request may be applied by hand rather than merged.

Before sending a patch, run the offline tests (Python 3.12+; no credentials, no network):

```bash
pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
python -m pytest --ignore=tests/test_browser_grid.py
```

**Never include real data** in an issue or a patch: no order ids, tracking numbers, addresses,
failure dossiers from `logs/failures/`, or anything from `config.json`, `.state.json` or `.env`.
Replace them with obviously fake values first. Security problems go through
[SECURITY.md](SECURITY.md), not a public issue.
