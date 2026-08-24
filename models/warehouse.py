import re

from pydantic import BaseModel, Field, model_validator


def normalize_address(text: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with a single space, and trim.

    Both jig substrings and the delivery address are normalized through this ONE function, so per-
    retailer punctuation/spacing differences ("c/o" vs "c o", "Ste." vs "Ste", commas, double spaces)
    can't defeat a match.
    """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


class InsuranceAddress(BaseModel):
    """A real postal address, in the exact shape BFMR's `POST /api/v2/insurance/file` wants.

    Field names mirror their form keys (`address[address_1]` and friends) so there is no translation
    layer to get wrong. EVERY field is optional to BFMR — an omitted one falls back to whatever is
    saved on the account profile — but a field that is blank in BOTH places fails the request naming
    itself, e.g. {"message": "Address Line 1 is required."}.

    `state` and `country` are validated here rather than discovered at filing time, because their
    documented formats are exactly the ones a person gets wrong: `state` is the ISO 3166-2 code
    WITHOUT the country prefix ("NH", not "US-NH" and not "New Hampshire"), and `country` is ISO
    3166-1 **alpha-3** ("USA", not "US"). Both mistakes look right and both 400 — on a path that
    spends money and, until it succeeds, leaves a package uninsured.
    """

    address_1: str = ""
    address_2: str = ""
    city: str = ""
    state: str = ""
    country: str = "USA"
    zip: str = ""

    @model_validator(mode="after")
    def _check_codes(self):
        if self.state and not re.fullmatch(r"[A-Z0-9]{1,3}", self.state):
            raise ValueError(
                f"state {self.state!r} must be the ISO 3166-2 code without the country prefix "
                f'(New Hampshire is "NH", not "US-NH" and not "New Hampshire").'
            )
        if self.country and not re.fullmatch(r"[A-Z]{3}", self.country):
            raise ValueError(
                f'country {self.country!r} must be ISO 3166-1 alpha-3 ("USA", not "US").'
            )
        if self.zip and not re.fullmatch(r"[0-9-]{5,10}", self.zip):
            raise ValueError(
                f"zip {self.zip!r} must be 5-10 characters of digits and hyphens only."
            )
        return self

    def as_form_fields(self) -> dict[str, str]:
        """`{"address[city]": "Testville", ...}` for the non-blank fields only.

        Blank fields are OMITTED rather than sent empty: omitting one lets BFMR fall back to the
        profile value, whereas sending "" is a value, and would overwrite good profile data with
        nothing.
        """
        return {
            f"address[{name}]": value
            for name, value in (
                ("address_1", self.address_1), ("address_2", self.address_2),
                ("city", self.city), ("state", self.state),
                ("country", self.country), ("zip", self.zip),
            )
            if str(value).strip()
        }


class Jig(BaseModel):
    """One address variant a buying group tells you to ship to (a "jig").

    Matching is normalized substring/keyword: a jig matches a delivery address only when ALL of its
    non-blank substring fields appear in the normalized address (see config.warehouses.classify_address
    for the normalization). `street` / `zip` / `name_contains` are convenience fields; `contains` is a
    generic list of extra required substrings. `label` is descriptive only (not written to the ledger in
    v1 — a single "Buying Group" column carries the group name).

    A jig with no substring fields would match every address, silently tagging personal orders with a
    buying group — so the validator rejects it. Configure at least one of street/zip/name_contains/contains.
    """

    label: str = ""
    street: str = ""
    zip: str = ""
    name_contains: str = ""
    contains: list[str] = Field(default_factory=list)
    #: Which of the buying group's REAL warehouse addresses this jig actually delivers to, by key
    #: into `Warehouse.insurance_addresses`. Blank = no mapping, and the insurer is told nothing.
    #:
    #: A jig is deliberately misspelled — BFMR hands out variants like "THIRTEEN SAMMPLE DR1VE"
    #: so each order routes distinctly — so it is a routing token, NOT a postal address. Filing
    #: insurance against it would put a fictional street on the policy, which is the kind of detail
    #: a claim is refused over. This is the pointer back to the real one.
    insure_as: str = ""

    @model_validator(mode="after")
    def _require_a_substring(self):
        if not self.required_substrings():
            raise ValueError(
                f"Jig {self.label or '(unlabeled)'} has no match fields; set at least one of "
                "street/zip/name_contains/contains, or it would match every address."
            )
        return self

    def required_substrings(self) -> list[str]:
        """The non-blank substrings that must ALL be present for this jig to match, normalized the
        SAME way as the address (see normalize_address) so the comparison is apples-to-apples."""
        out: list[str] = []
        for value in (self.street, self.zip, self.name_contains, *self.contains):
            cleaned = normalize_address(value)
            if cleaned:
                out.append(cleaned)
        return out


class Warehouse(BaseModel):
    """A buying group and the set of jigs (address variants) that route an order to it.

    `buying_group` is the value written to the ledger's "Buying Group" column. Name a group literally
    `Personal` to get an explicit `Personal` tag for your own reship address(es); any address matching no
    jig at all is tagged `Unclassified` (not silently personal).
    """

    buying_group: str
    jigs: list[Jig] = Field(default_factory=list)
    #: The group's REAL warehouse addresses, keyed by a short name a jig's `insure_as` refers to.
    #: Only used for insurance filing; routing still happens on the jig substrings above.
    insurance_addresses: dict[str, InsuranceAddress] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _insure_as_names_a_real_address(self):
        """A jig pointing at a key that does not exist is a silent downgrade, so refuse it at load.

        Nothing would break loudly otherwise: the lookup would miss, no address would be sent, and
        BFMR would quietly fall back to the profile address — filing every package from that jig
        against the wrong warehouse until someone noticed on a claim.
        """
        unknown = sorted({
            jig.insure_as for jig in self.jigs
            if jig.insure_as and jig.insure_as not in self.insurance_addresses
        })
        if unknown:
            raise ValueError(
                f"{self.buying_group}: jig insure_as {unknown} names no entry in "
                f"insurance_addresses (have: {sorted(self.insurance_addresses) or 'none'})."
            )
        return self
