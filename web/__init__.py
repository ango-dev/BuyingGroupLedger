"""A read-only local web dashboard for the ledger (phase 1).

It never calls a retailer or buying-group API, never runs a scrape and never changes the ledger
schema; its one write path is the Orders page's editor (web/ledger_writer.py). Every read goes
through `web.ledger_reader` -- a CSV backup for development and tests, or the ledger file itself.
See docs/operations.md,
"The web dashboard".
"""
