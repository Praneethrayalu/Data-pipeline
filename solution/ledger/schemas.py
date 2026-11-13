"""Loading of workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalise import normalise_header


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple[str, ...] = ()
    default: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[Field]
    # normalised header -> field; built from field names and aliases
    header_map: dict[str, Field] = field(default_factory=dict)
    reference_field: Field | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkflowSchema":
        fields = [
            Field(
                name=f["name"],
                type=f["type"],
                required=bool(f.get("required")),
                pii=bool(f.get("pii")),
                reference=bool(f.get("reference")),
                values=tuple(f.get("values", ())),
                default=f.get("default"),
                aliases=tuple(f.get("aliases", ())),
            )
            for f in raw["fields"]
        ]
        schema = cls(workflow_id=raw["workflow_id"], fields=fields)
        for f in fields:
            schema.header_map.setdefault(normalise_header(f.name), f)
            for alias in f.aliases:
                schema.header_map.setdefault(normalise_header(alias), f)
            if f.reference:
                schema.reference_field = f
        return schema


def load_schemas(schemas_dir: Path) -> dict[str, WorkflowSchema]:
    """Load every workflow schema (*.json) in the schemas directory."""
    schemas: dict[str, WorkflowSchema] = {}
    for path in sorted(schemas_dir.glob("*.json")):
        schema = WorkflowSchema.from_dict(json.loads(path.read_text(encoding="utf-8")))
        schemas[schema.workflow_id] = schema
    return schemas


@dataclass(frozen=True)
class Disposition:
    code: str
    is_terminal: bool
    is_success: bool


def load_dispositions(schemas_dir: Path) -> dict[str, dict[str, Disposition]]:
    """workflow_id -> {code -> Disposition} from dispositions.csv."""
    catalogue: dict[str, dict[str, Disposition]] = {}
    with open(schemas_dir / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            catalogue.setdefault(row["workflow_id"], {})[row["code"]] = Disposition(
                code=row["code"],
                is_terminal=row["is_terminal"].strip().lower() == "true",
                is_success=row["is_success"].strip().lower() == "true",
            )
    return catalogue
