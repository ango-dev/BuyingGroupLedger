"""
Receipt files check: every receipt file on disk against what the ledger and the Taxes page still
point at.

What it lists:
  - ORPHANS: files under the order receipts directory no ledger row's Receipt Link names, and
    files under data/expenses no expense record names;
  - MISSING: records whose file is gone (the link is dead; re-upload or clear it);
  - TWINS: expense receipts that still share one bare name (uploaded before names were numbered).
DRY RUN BY DEFAULT -- reads everything and deletes nothing:
    python -m scripts.receipt_files
Apply: delete the orphans and number the twins (Order.pdf -> Order-0001.pdf, Order-0002.pdf):
    python -m scripts.receipt_files --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import tax_inputs  # noqa: E402


def plan(*, ledger_links: set[str], receipts_root: Path, all_inputs: dict, expenses_root: Path) -> dict:
    """The pure part: {orphans: [Path], missing: [(year, id, rel)], twins: {bare name: [(year, id)]}}."""
    orphans: list[Path] = []
    linked = {link[len("/receipts/"):] for link in ledger_links if str(link).startswith("/receipts/")}
    if receipts_root.is_dir():
        for path in sorted(receipts_root.rglob("*")):
            if path.is_file() and not path.name.endswith(".part"):
                if path.relative_to(receipts_root).as_posix() not in linked:
                    orphans.append(path)
    referenced: set[str] = set()
    missing: list[tuple[int, str, str]] = []
    names: dict[str, list[tuple[int, str]]] = {}
    for year, inputs in sorted(all_inputs.items()):
        for e in inputs.expenses:
            receipt = e.get("receipt") or {}
            rel = str(receipt.get("file") or "")
            if rel:
                referenced.add(rel)
                if not (expenses_root.parent / rel).is_file():
                    missing.append((year, e["id"], rel))
            name = str(receipt.get("name") or "")
            if rel and name and not tax_inputs._SUFFIXED.match(name):
                names.setdefault(name, []).append((year, e["id"]))
    if expenses_root.is_dir():
        for path in sorted(expenses_root.rglob("*")):
            if path.is_file() and path.relative_to(expenses_root.parent).as_posix() not in referenced:
                orphans.append(path)
    twins = {name: owners for name, owners in names.items() if len(owners) > 1}
    return {"orphans": orphans, "missing": missing, "twins": twins}


def apply(report: dict, *, path: Path, data_dir: Path) -> dict:
    """Delete the orphans; number the twins (each file moved, its year saved). Returns counts."""
    deleted = 0
    for file in report["orphans"]:
        try:
            file.unlink()
            deleted += 1
        except OSError:
            pass
    numbered = 0
    for name, owners in report["twins"].items():
        stem, ext = tax_inputs._receipt_base(name)
        everything = tax_inputs.load_all(path)
        n = 0
        for year, entry_id in owners:
            entry = next((e for e in everything[year].expenses if e["id"] == entry_id), None)
            if entry is None:
                continue
            n += 1
            new_name = tax_inputs._numbered(stem, ext, n)
            old_rel = entry["receipt"].get("file") or ""
            if old_rel:
                new_rel = Path(old_rel).parent / f"{entry_id}_{new_name}"
                try:
                    (Path(data_dir) / old_rel).rename(Path(data_dir) / new_rel)
                    entry["receipt"]["file"] = new_rel.as_posix()
                except OSError:
                    continue
            entry["receipt"]["name"] = new_name
            tax_inputs.save_year(path, year, everything[year])
            numbered += 1
    return {"deleted": deleted, "numbered": numbered}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="delete the orphans and number the twins")
    args = parser.parse_args(argv)

    from ledger.sync import HEADER, _get_worksheet
    from receipts import store

    values = _get_worksheet().get_all_values()
    col = HEADER.index("Receipt Link")
    links = {row[col].strip() for row in values[1:] if len(row) > col and row[col].strip()}
    data_dir = ROOT / "data"
    path = data_dir / tax_inputs.FILE_NAME
    report = plan(ledger_links=links, receipts_root=store.receipts_dir(), all_inputs=tax_inputs.load_all(path),
                  expenses_root=data_dir / tax_inputs.EXPENSES_DIR)
    print(f"{len(report['orphans'])} orphan file(s), {len(report['missing'])} missing file(s), {len(report['twins'])} twin name(s)")
    for file in report["orphans"]:
        print(f"  orphan   {file}")
    for year, entry_id, rel in report["missing"]:
        print(f"  missing  {year} {entry_id}: {rel}")
    for name, owners in report["twins"].items():
        print(f"  twins    {name}: " + ", ".join(f"{y}/{i}" for y, i in owners))
    if not args.apply:
        print("dry run: nothing deleted or renamed (add --apply)")
        return 0
    counts = apply(report, path=path, data_dir=data_dir)
    print(f"deleted {counts['deleted']} orphan file(s), numbered {counts['numbered']} twin(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
