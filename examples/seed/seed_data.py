"""Load seed-pack YAML and build deterministic titles/metadata (from dspace-seed, example-local)."""

from __future__ import annotations

# httpx default in create_validated_client is 30s; public demo is often slower (cold start, TLS).
DEFAULT_SEED_HTTP_TIMEOUT = 120.0

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# --- Models (same shape as dspace-seed generators/seed_pack.py) ---


@dataclass
class Discipline:
    code: str
    name: str
    subfields: list[str]


@dataclass
class Scientist:
    first: str
    last: str
    slug: str


@dataclass
class Work:
    title: str
    discipline: str
    subfield: str
    authors: list[str]


@dataclass
class SeedPack:
    version: int
    description: str
    disciplines: list[Discipline]
    scientists: list[Scientist]
    works: list[Work]
    group_presets: list[str]

    def get_discipline_by_code(self, code: str) -> Discipline | None:
        for disc in self.disciplines:
            if disc.code == code:
                return disc
        return None

    def get_works_for_discipline(
        self, discipline_code: str, subfield: str | None = None
    ) -> list[Work]:
        works = [w for w in self.works if w.discipline == discipline_code]
        if subfield:
            works = [w for w in works if w.subfield == subfield]
        return works


def load_seed_pack(path: Path) -> SeedPack:
    """Load seed pack from YAML (same schema as dspace-seed seedpacks/default.yml)."""
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("Seed pack must be a YAML mapping at the root")
    disciplines = [Discipline(**d) for d in data.get("disciplines") or []]
    scientists = [Scientist(**s) for s in data.get("scientists") or []]
    works = [Work(**w) for w in data.get("works") or []]
    return SeedPack(
        version=int(data["version"]),
        description=str(data.get("description") or ""),
        disciplines=disciplines,
        scientists=scientists,
        works=works,
        group_presets=list(data.get("group_presets") or []),
    )


# --- DataFactory (from dspace-seed generators/factories.py) ---


class DataFactory:
    """Factory for creating deterministic DSpace content from a seed pack."""

    def __init__(self, seed_pack: SeedPack, seed: int = 42) -> None:
        self.seed_pack = seed_pack
        self.seed = seed
        self.rng = random.Random(seed)

    def get_first_discipline(self) -> Discipline:
        if not self.seed_pack.disciplines:
            raise ValueError("No disciplines in seed pack")
        return self.seed_pack.disciplines[0]

    def get_discipline_title(self, discipline: Discipline) -> str:
        return discipline.name

    def get_collection_title(self, discipline: Discipline, subfield_index: int = 0) -> str:
        if not discipline.subfields:
            return f"{discipline.name}: General"
        subfield = discipline.subfields[subfield_index % len(discipline.subfields)]
        return f"{discipline.name}: {subfield}"

    def get_item_title(self, discipline: Discipline, subfield_index: int = 0) -> str:
        if not discipline.subfields:
            subfield = "General"
        else:
            subfield = discipline.subfields[subfield_index % len(discipline.subfields)]

        works = self.seed_pack.get_works_for_discipline(discipline.code, subfield)
        if works:
            return works[0].title
        return f"Studies in {subfield}: An Introduction"

    def get_item_metadata(
        self, title: str, discipline: Discipline, subfield_index: int = 0
    ) -> dict[str, Any]:
        subfield = (
            discipline.subfields[subfield_index % len(discipline.subfields)]
            if discipline.subfields
            else "General"
        )
        works = self.seed_pack.get_works_for_discipline(discipline.code, subfield)

        authors: list[dict[str, Any]] = []
        if works:
            matching_work = next((w for w in works if w.title == title), None)
            if matching_work and matching_work.authors:
                for author_name in matching_work.authors:
                    authors.append(
                        {
                            "value": author_name,
                            "language": "en",
                            "authority": None,
                            "confidence": -1,
                        }
                    )

        if not authors:
            scientist_index = self.rng.randint(0, len(self.seed_pack.scientists) - 1)
            scientist = self.seed_pack.scientists[scientist_index]
            authors.append(
                {
                    "value": f"{scientist.last}, {scientist.first}",
                    "language": "en",
                    "authority": None,
                    "confidence": -1,
                }
            )

        authors.append(
            {
                "value": "Luyten, Bram",
                "language": "en",
                "authority": None,
                "confidence": -1,
            }
        )

        return {
            "dc.title": [
                {
                    "value": title,
                    "language": "en",
                    "authority": None,
                    "confidence": -1,
                }
            ],
            "dc.contributor.author": authors,
            "dc.type": [
                {
                    "value": "Journal Article",
                    "language": "en",
                    "authority": None,
                    "confidence": -1,
                }
            ],
            "dc.description.abstract": [
                {
                    "value": (
                        f"A comprehensive study in the field of {discipline.name}. "
                        "Content generated by dspace-python-client seed examples."
                    ),
                    "language": "en",
                    "authority": None,
                    "confidence": -1,
                }
            ],
            "dc.subject": [
                {
                    "value": discipline.name,
                    "language": "en",
                    "authority": None,
                    "confidence": -1,
                }
            ],
        }

    def generate_sample_pdf_content(self, title: str) -> bytes:
        """Minimal valid PDF bytes with title on the page."""
        pdf_content = f"""%PDF-1.4
1 0 obj
<<
/Type /Catalog
/Pages 2 0 R
>>
endobj
2 0 obj
<<
/Type /Pages
/Kids [3 0 R]
/Count 1
>>
endobj
3 0 obj
<<
/Type /Page
/Parent 2 0 R
/MediaBox [0 0 612 792]
/Contents 4 0 R
/Resources <<
/Font <<
/F1 <<
/Type /Font
/Subtype /Type1
/BaseFont /Helvetica
>>
>>
>>
>>
endobj
4 0 obj
<<
/Length 55
>>
stream
BT
/F1 12 Tf
50 750 Td
({title}) Tj
ET
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000317 00000 n 
trailer
<<
/Size 5
/Root 1 0 R
>>
startxref
420
%%EOF
"""
        return pdf_content.encode("latin-1")


def generate_unique_email(first_name: str, last_name: str) -> str:
    """Unique email for MegaSpace EPeople (same scheme as dspace-seed)."""
    import secrets
    import string

    first = first_name.lower().replace(" ", "")
    last = last_name.lower().replace(" ", "")
    chars = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(5))
    return f"{first}.{last}.{suffix}@megaspace.atmire.com"


def build_mega_metadata(
    factory: DataFactory,
    seed_pack: SeedPack,
    title: str,
    discipline: Discipline,
) -> dict[str, Any]:
    """Stress-test metadata (aligned with dspace-seed megaspace)."""
    metadata = factory.get_item_metadata(title, discipline, 0)

    subjects = []
    for i in range(100):
        subjects.append(
            {
                "value": f"{discipline.name} keyword {i + 1}",
                "language": "en",
                "authority": None,
                "confidence": -1,
            }
        )
    metadata["dc.subject"] = subjects

    extra_contributors = []
    for scientist in seed_pack.scientists[:50]:
        extra_contributors.append(
            {
                "value": f"{scientist.last}, {scientist.first}",
                "language": "en",
                "authority": None,
                "confidence": -1,
            }
        )
    metadata["dc.contributor"] = extra_contributors

    descriptions = []
    for i in range(50):
        descriptions.append(
            {
                "value": (f"This is description number {i + 1} for the mega-metadata test item. ")
                * 5,
                "language": "en",
                "authority": None,
                "confidence": -1,
            }
        )
    metadata["dc.description"] = descriptions

    dates = []
    for year in range(2000, 2025):
        dates.append(
            {
                "value": f"{year}-01-01",
                "language": None,
                "authority": None,
                "confidence": -1,
            }
        )
    metadata["dc.date.issued"] = dates

    identifiers = []
    for i in range(25):
        identifiers.append(
            {
                "value": f"urn:test:megametadata:{i + 1:05d}",
                "language": None,
                "authority": None,
                "confidence": -1,
            }
        )
    metadata["dc.identifier.other"] = identifiers

    return metadata
