#!/usr/bin/env python
"""Populate an isolated database with synthetic conversations and usage.

Writes to a scratch home directory, never the user's real one, so it is safe to run
repeatedly. Used by `python tasks.py seed-test-data` and by the dev sandbox.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from localai.config.paths import AppPaths  # noqa: E402
from localai.domain.messages import Message, Role, TokenSource, Usage  # noqa: E402
from localai.storage.conversations import ConversationStore  # noqa: E402
from localai.storage.db import Database  # noqa: E402
from localai.storage.usage import UsageStore  # noqa: E402

TOPICS = [
    ("Reviewing the archive drive", "Which folders on D: hold financial records?"),
    ("Refactoring the parser", "Can you find every call site of parse_header?"),
    ("Photo deduplication", "Find duplicate images under D:/Photos"),
    ("Understanding an old project", "What does this codebase actually do?"),
    ("Drafting release notes", "Summarise the changes since last week"),
]
MODELS = ["qwen3:8b", "qwen2.5:7b", "mock-tools:8b"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="scratch home (default: <root>/.devdata)")
    parser.add_argument("--conversations", type=int, default=8)
    args = parser.parse_args()

    home = args.home or ROOT / ".devdata"
    paths = AppPaths.resolve(home).ensure()
    database = Database(paths.database)
    conversations = ConversationStore(database)
    usage = UsageStore(database)
    random.seed(20260718)  # deterministic: the same seed data every run

    created = 0
    for index in range(args.conversations):
        title, prompt = TOPICS[index % len(TOPICS)]
        model = MODELS[index % len(MODELS)]
        record = conversations.create(
            title=f"{title} #{index + 1}", model=model, workspace=str(ROOT),
            system_prompt="You are a careful local assistant.",
        )
        conversations.add_message(record.id, Message(role=Role.USER, content=prompt))
        conversations.add_message(
            record.id,
            Message(role=Role.ASSISTANT, content=f"Here is what I found about {title.lower()}."),
        )
        # A mix of reported and unreported counts, so usage views exercise both.
        reported = index % 3 != 0
        usage.record(
            Usage(
                prompt_tokens=random.randint(200, 3000),
                completion_tokens=random.randint(50, 800),
                token_source=TokenSource.REPORTED if reported else TokenSource.UNKNOWN,
                total_duration_ns=random.randint(1, 30) * 1_000_000_000,
                eval_duration_ns=random.randint(1, 20) * 1_000_000_000,
            ),
            model=model, conversation_id=record.id, workspace=str(ROOT),
            tool_calls=random.randint(0, 5), message_count=2,
        )
        created += 1

    print(f"Seeded {created} conversations into {paths.database}")
    print(f"Use it with:  localai --home {home} history list")
    database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
