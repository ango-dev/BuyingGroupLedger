import json
from pathlib import Path

from models.profile import ProfileConfig

PROFILES_FILE = Path(__file__).resolve().parent.parent / "profiles.json"


def load_profiles() -> list[ProfileConfig]:
    """Load all configured Browser-Use profiles (see profiles.example.json)."""
    if not PROFILES_FILE.exists():
        return []

    data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    return [ProfileConfig.model_validate(entry) for entry in data]


def load_profiles_for_retailer(retailer_key: str) -> list[ProfileConfig]:
    """Every configured profile that's logged into the given retailer."""
    return [p for p in load_profiles() if retailer_key in p.retailers]


def save_profiles(profiles: list[ProfileConfig]) -> None:
    """Write profiles back to profiles.json (used by scripts/create_profile.py)."""
    # mode="json" coerces any non-JSON-native types (UUID, etc.) to serializable ones.
    data = [p.model_dump(mode="json") for p in profiles]
    PROFILES_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
