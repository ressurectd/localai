#!/usr/bin/env python
"""Regenerate schemas/*.json from the pydantic models.

Run via `python tasks.py schemas` whenever a config or rule model changes. The
generated files are committed so external agents can read them without running
anything, and `tasks.py validate-docs` fails if they drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from localai.config.models import Config, ModelProfile, PermissionRuleModel  # noqa: E402
from localai.version import (  # noqa: E402
    CONFIG_SCHEMA_VERSION,
    PERMISSION_SCHEMA_VERSION,
)

DRAFT = "https://json-schema.org/draft/2020-12/schema"
OUT = ROOT / "schemas"


def emit(name: str, schema: dict) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"wrote schemas/{name}")


def main() -> int:
    config = Config.model_json_schema()
    config["$schema"] = DRAFT
    config["$id"] = "https://localai.local/schemas/config.schema.json"
    config["title"] = "localai configuration"
    config["description"] = (
        f"Schema version {CONFIG_SCHEMA_VERSION}. Generated from localai.config.models.Config "
        "-- do not hand-edit; run `python tasks.py schemas`."
    )
    emit("config.schema.json", config)

    rule = PermissionRuleModel.model_json_schema()
    rule["$schema"] = DRAFT
    rule["$id"] = "https://localai.local/schemas/permission-rule.schema.json"
    rule["description"] = (
        f"Schema version {PERMISSION_SCHEMA_VERSION}. Evaluation order: "
        "docs/permissions-engine.md."
    )
    emit("permission-rule.schema.json", rule)

    profile = ModelProfile.model_json_schema()
    profile["$schema"] = DRAFT
    profile["$id"] = "https://localai.local/schemas/model-profile.schema.json"
    emit("model-profile.schema.json", profile)

    print("\nHand-written contracts (tool-definition, tool-result, plugin-manifest) are not")
    print("regenerated; they are asserted against the code by tests/unit/test_schemas.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
