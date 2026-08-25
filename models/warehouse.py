import re

from pydantic import BaseModel, Field, model_validator


def normalize_address(text: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with a single space, and trim.

    Both jig substrings and the delivery address are normalized through this ONE function, so per-
    retailer punctuation/spacing differences ("c/o" vs "c o", "Ste." vs "Ste", commas, double spaces)
    can't defeat a match.
    """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


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
