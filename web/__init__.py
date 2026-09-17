"""A read-only local web dashboard for the ledger (phase 1).

It never writes the Google Sheet, never calls a retailer or buying-group API, never runs a scrape
and never changes the ledger schema. Every read goes through `web.ledger_reader` -- a CSV backup
for development and tests, or the live Sheet through the READONLY scope. See docs/operations.md,
"The web dashboard".
"""
