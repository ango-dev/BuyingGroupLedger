from config.loader import config_section, load_config, save_config
from models.profile import ProfileConfig

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
    """Load all configured Browser-Use profiles from config.json's `profiles` section."""
    profiles = [ProfileConfig.model_validate(entry) for entry in config_section("profiles")]
    for profile in profiles:
        _check_no_conflicting_retailers(profile)
    return profiles


def load_profiles_for_retailer(retailer_key: str) -> list[ProfileConfig]:
    """Every configured profile that's logged into the given retailer."""
    return [p for p in load_profiles() if retailer_key in p.retailers]


def save_profiles(profiles: list[ProfileConfig]) -> None:
    """Write the `profiles` section back (used by scripts/create_profile.py for the profile_id).

    Rewrites config.json with ONLY that key replaced, so every other section — and the `"//"` comment
    keys the template uses — survives untouched. This is the one place the app writes the file a
    human authors, which is why it is a targeted swap rather than a regeneration.
    """
    # mode="json" coerces any non-JSON-native types (UUID, etc.) to serializable ones.
    config = load_config()
    config["profiles"] = [p.model_dump(mode="json") for p in profiles]
    save_config(config)
