import csv
from datetime import datetime, timezone
from pathlib import Path

from models.order import FIELDNAMES, OrderItem


def write_csv(items: list[OrderItem], output_dir: Path | str = "data") -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    last_scraped_at = datetime.now(timezone.utc).isoformat()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"orders_{timestamp}.csv"

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for item in items:
            row = item.model_dump()
            row["last_scraped_at"] = last_scraped_at
            writer.writerow(row)

    return path
