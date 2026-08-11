import json
from pathlib import Path

from models.profile import ProfileConfig

PROFILES_FILE = Path(__file__).resolve().parent.parent / "profiles.json"

# A profile is ONE Browser-Use browser identity behind ONE proxy/IP. Amazon (consumer) and Amazon
# Business are two SEPARATE Amazon accounts, so putting both retailer keys on one profile would sit
# both accounts behind the same identity/IP — exactly the linking/flagging risk that the separate
# business profile (with its own dedicated proxy) exists to avoid. Keep them on different profiles.
_MUTUALLY_EXCLUSIVE_RETAILERS = frozenset({"amazon", "amazon-business"})


def _check_no_conflicting_retailers(profile: ProfileConfig) -> None:
    """Fail loud if a single profile lists both Amazon and Amazon Business.

    A silent misconfig here cross-contaminates two Amazon accounts on one identity, so raise rather
    than warn — a bad profiles.json should stop the run, not scrape the wrong account quietly.
    """
    conflict = _MUTUALLY_EXCLUSIVE_RETAILERS & set(profile.retailers)
    if len(conflict) > 1:
        raise ValueError(
            f"Profile '{profile.label}' lists conflicting retailers {sorted(conflict)}: "
            "'amazon' and 'amazon-business' are separate Amazon accounts and must live on separate "
            "profiles (each with its own profile_id + dedicated proxy), never on one."
        )


def load_profiles() -> list[ProfileConfig]:
    """Load all configured Browser-Use profiles (see profiles.example.json)."""
    if not PROFILES_FILE.exists():
        return []

    data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    profiles = [ProfileConfig.model_validate(entry) for entry in data]
    for profile in profiles:
        _check_no_conflicting_retailers(profile)
    return profiles


def load_profiles_for_retailer(retailer_key: str) -> list[ProfileConfig]:
    """Every configured profile that's logged into the given retailer."""
    return [p for p in load_profiles() if retailer_key in p.retailers]


def save_profiles(profiles: list[ProfileConfig]) -> None:
    """Write profiles back to profiles.json (used by scripts/create_profile.py)."""
    # mode="json" coerces any non-JSON-native types (UUID, etc.) to serializable ones.
    data = [p.model_dump(mode="json") for p in profiles]
    PROFILES_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
