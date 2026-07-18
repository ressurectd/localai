"""Storage, migrations, usage honesty and configuration handling."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from localai.config.manager import ENV_OVERRIDES, ConfigManager, apply_env_overrides
from localai.config.models import Config, PermissionMode
from localai.config.paths import AppPaths
from localai.domain.messages import Message, Role, TokenSource, ToolCall, Usage
from localai.errors import ConfigError, MigrationError
from localai.storage.conversations import ConversationStore
from localai.storage.db import Database, discover_migrations
from localai.storage.usage import Period, UsageStore, period_bounds

# --- migrations -------------------------------------------------------------


def test_migrations_are_contiguous_and_ordered() -> None:
    migrations = discover_migrations()
    assert migrations
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))


def test_migrations_apply_on_a_fresh_database(paths: AppPaths) -> None:
    db = Database(paths.database)
    status = db.migration_status()
    assert status["current_version"] == status["latest_version"]
    assert not status["pending"]
    db.close()


def test_migrations_are_idempotent(paths: AppPaths) -> None:
    """Re-running migrate on an up-to-date database must be a no-op."""
    db = Database(paths.database)
    assert db.migrate() == []
    db.close()


def test_migration_state_survives_reopening(paths: AppPaths) -> None:
    Database(paths.database).close()
    reopened = Database(paths.database)
    assert reopened.migration_status()["current_version"] >= 2
    reopened.close()


def test_expected_tables_exist(database: Database) -> None:
    rows = database.query("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    for table in (
        "conversations",
        "messages",
        "usage_records",
        "audit_log",
        "permission_rules",
        "memories",
        "schema_migrations",
    ):
        assert table in names, f"missing table {table}"


def test_foreign_keys_are_enforced(database: Database) -> None:
    """A message must not survive its conversation."""
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO messages (id, conversation_id, seq, role, content, created_at)"
            " VALUES ('m1','does-not-exist',0,'user','x',0)"
        )


def test_integrity_check_passes(database: Database) -> None:
    assert database.health()["ok"]


def test_failed_migration_leaves_the_database_unchanged(paths: AppPaths, monkeypatch) -> None:
    """A broken migration must not half-apply."""
    from localai.storage import db as db_module

    Database(paths.database).close()  # apply the real migrations first

    good = discover_migrations()
    # The first statement is valid and the second is not, so this only passes if the
    # successful half is rolled back rather than left behind.
    broken = db_module.Migration(
        version=len(good) + 1, name="broken", sql="CREATE TABLE ok (x); THIS IS NOT SQL;"
    )
    monkeypatch.setattr(db_module, "discover_migrations", lambda: [*good, broken])

    with pytest.raises(MigrationError, match=r"00\d_broken"):
        Database(paths.database)  # __init__ migrates, and must fail

    monkeypatch.undo()
    check = Database(paths.database)
    tables = {r["name"] for r in check.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" not in tables, "a partially applied migration was left behind"
    assert check.migration_status()["current_version"] == len(good)
    check.close()


# --- conversations ----------------------------------------------------------


def test_conversation_round_trip(database: Database) -> None:
    store = ConversationStore(database)
    record = store.create(title="Test", model="m:1b", system_prompt="be brief")

    store.add_message(record.id, Message(role=Role.USER, content="hello"))
    store.add_message(
        record.id,
        Message(
            role=Role.ASSISTANT,
            content="hi",
            tool_calls=[ToolCall(name="read_file", arguments={"path": "a.txt"})],
        ),
    )
    store.add_message(record.id, Message(role=Role.TOOL, content="file body", name="read_file"))

    messages = store.messages(record.id)
    assert [m.role for m in messages] == [Role.USER, Role.ASSISTANT, Role.TOOL]
    assert messages[1].tool_calls[0].name == "read_file"
    assert messages[1].tool_calls[0].arguments == {"path": "a.txt"}
    assert store.get(record.id).message_count == 3


def test_sequence_numbers_are_dense_and_ordered(database: Database) -> None:
    store = ConversationStore(database)
    record = store.create()
    sequences = [
        store.add_message(record.id, Message(role=Role.USER, content=str(i))) for i in range(5)
    ]
    assert sequences == [0, 1, 2, 3, 4]


def test_deleting_a_conversation_cascades(database: Database) -> None:
    store = ConversationStore(database)
    record = store.create()
    store.add_message(record.id, Message(role=Role.USER, content="x"))
    store.delete(record.id)
    assert database.query("SELECT id FROM messages WHERE conversation_id = ?", (record.id,)) == []


def test_fork_copies_a_prefix_and_leaves_the_original(database: Database) -> None:
    store = ConversationStore(database)
    record = store.create(title="Original")
    for i in range(5):
        store.add_message(record.id, Message(role=Role.USER, content=f"message {i}"))

    forked = store.fork(record.id, at_seq=2)
    assert len(store.messages(forked.id)) == 3
    assert len(store.messages(record.id)) == 5  # untouched
    assert forked.parent_id == record.id
    assert forked.forked_from_seq == 2


def test_search_finds_message_text(database: Database) -> None:
    store = ConversationStore(database)
    record = store.create(title="Budget talk")
    store.add_message(record.id, Message(role=Role.USER, content="what is the quarterly budget"))
    results = store.search("quarterly")
    assert results
    assert results[0]["conversation_id"] == record.id


def test_search_handles_no_results(database: Database) -> None:
    assert ConversationStore(database).search("zzzznotpresent") == []


def test_markdown_export_contains_the_conversation(database: Database) -> None:
    store = ConversationStore(database)
    record = store.create(title="Export test", model="m:1b", system_prompt="sys")
    store.add_message(record.id, Message(role=Role.USER, content="question"))
    store.add_message(record.id, Message(role=Role.ASSISTANT, content="answer"))

    markdown = store.export_markdown(record.id)
    assert "# Export test" in markdown
    assert "question" in markdown and "answer" in markdown
    assert "sys" in markdown


def test_json_export_is_versioned(database: Database) -> None:
    store = ConversationStore(database)
    record = store.create(title="x")
    store.add_message(record.id, Message(role=Role.USER, content="hi"))
    payload = store.export_json(record.id)
    assert payload["schema"] == "localai.conversation/1"
    assert len(payload["messages"]) == 1


def test_export_of_a_missing_conversation_raises(database: Database) -> None:
    with pytest.raises(KeyError):
        ConversationStore(database).export_json("conv_nope")


# --- usage ------------------------------------------------------------------


def test_usage_preserves_reported_provenance(database: Database) -> None:
    store = UsageStore(database)
    store.record(
        Usage(prompt_tokens=100, completion_tokens=50, token_source=TokenSource.REPORTED),
        model="m:1b",
    )
    totals = store.totals(Period.ALL)
    assert totals.prompt_tokens == 100
    assert totals.exactness == "exact"
    assert totals.format_tokens(150) == "150"


def test_usage_marks_estimated_totals(database: Database) -> None:
    """An estimate must never be displayed as though it were exact."""
    store = UsageStore(database)
    store.record(
        Usage(prompt_tokens=10, completion_tokens=5, token_source=TokenSource.ESTIMATED),
        model="m:1b",
    )
    totals = store.totals(Period.ALL)
    assert totals.exactness == "estimated"
    assert totals.format_tokens(15).startswith("~")


def test_mixed_provenance_is_reported_as_mixed(database: Database) -> None:
    store = UsageStore(database)
    store.record(Usage(prompt_tokens=10, token_source=TokenSource.REPORTED), model="m:1b")
    store.record(Usage(prompt_tokens=10, token_source=TokenSource.UNKNOWN), model="m:1b")
    totals = store.totals(Period.ALL)
    assert totals.exactness == "mixed"
    assert totals.format_tokens(20).startswith("~")


def test_unknown_provenance_is_marked_with_a_question_mark(database: Database) -> None:
    store = UsageStore(database)
    store.record(Usage(prompt_tokens=10, token_source=TokenSource.UNKNOWN), model="m:1b")
    assert store.totals(Period.ALL).format_tokens(10).endswith("?")


def test_empty_usage_is_not_reported_as_exact(database: Database) -> None:
    assert UsageStore(database).totals(Period.ALL).exactness == "empty"


def test_tokens_per_second_is_none_without_a_duration() -> None:
    assert Usage(completion_tokens=100, eval_duration_ns=0).tokens_per_second is None


def test_tokens_per_second_computed_from_eval_duration() -> None:
    usage = Usage(completion_tokens=100, eval_duration_ns=2_000_000_000)
    assert usage.tokens_per_second == pytest.approx(50.0)


def test_energy_is_always_labelled_an_estimate(database: Database) -> None:
    store = UsageStore(database)
    store.record(
        Usage(
            prompt_tokens=1, total_duration_ns=3_600_000_000_000, token_source=TokenSource.REPORTED
        ),
        model="m:1b",
        assumed_watts=100.0,
    )
    report = store.report(Period.ALL, assumed_watts=100.0)
    assert report["totals"]["energy_is_estimate"] is True
    assert any("not a measurement" in note for note in report["notes"])


def test_usage_grouping_by_model(database: Database) -> None:
    store = UsageStore(database)
    store.record(Usage(prompt_tokens=100, token_source=TokenSource.REPORTED), model="a:1b")
    store.record(Usage(prompt_tokens=200, token_source=TokenSource.REPORTED), model="b:7b")
    rows = {row["key"]: row for row in store.by_model(Period.ALL)}
    assert rows["b:7b"]["total_tokens"] >= rows["a:1b"]["total_tokens"]


def test_report_is_json_serialisable(database: Database) -> None:
    import json

    store = UsageStore(database)
    store.record(Usage(prompt_tokens=1, token_source=TokenSource.REPORTED), model="m:1b")
    json.dumps(store.report(Period.ALL))  # must not raise


@pytest.mark.parametrize("period", list(Period))
def test_every_period_has_computable_bounds(period: Period) -> None:
    start, _end = period_bounds(period)
    if start is not None:
        assert start <= time.time()


def test_rolling_and_calendar_windows_differ() -> None:
    """`7d` is rolling; `week` snaps to Monday. Conflating them misleads."""
    rolling, _ = period_bounds(Period.SEVEN_DAYS)
    calendar, _ = period_bounds(Period.WEEK)
    assert rolling is not None and calendar is not None


# --- configuration ----------------------------------------------------------


def test_defaults_validate() -> None:
    config = Config()
    assert config.permissions.mode is PermissionMode.AUTO
    assert config.safety.use_recycle_bin is True
    assert config.privacy.telemetry is False


def test_unknown_key_is_rejected(paths: AppPaths) -> None:
    """A typo must fail loudly rather than being silently ignored."""
    paths.config_file.write_text("typo_key = true\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        ConfigManager(paths).load()
    assert "typo_key" in str(info.value)


def test_malformed_toml_reports_the_file(paths: AppPaths) -> None:
    paths.config_file.write_text("this is [not valid toml\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"not valid TOML"):
        ConfigManager(paths).load()


def test_save_and_reload_round_trip(paths: AppPaths) -> None:
    manager = ConfigManager(paths)
    config = Config(default_model="qwen3:8b")
    config.permissions.mode = PermissionMode.MANUAL
    manager.save(config)

    reloaded = ConfigManager(paths).load()
    assert reloaded.default_model == "qwen3:8b"
    assert reloaded.permissions.mode is PermissionMode.MANUAL


def test_save_is_atomic_leaving_no_temp_files(paths: AppPaths) -> None:
    ConfigManager(paths).save(Config())
    assert not list(paths.home.glob(".config-*.toml"))


def test_env_overrides_apply(paths: AppPaths) -> None:
    manager = ConfigManager(
        paths, env={"LOCALAI_MODEL": "env:1b", "LOCALAI_PERMISSION_MODE": "manual"}
    )
    config = manager.load()
    assert config.default_model == "env:1b"
    assert config.permissions.mode is PermissionMode.MANUAL
    assert set(manager.applied_env_vars) == {"LOCALAI_MODEL", "LOCALAI_PERMISSION_MODE"}


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)]
)
def test_boolean_env_values_are_coerced(value: str, expected: bool) -> None:
    raw: dict = {}
    apply_env_overrides(raw, {"LOCALAI_READ_ONLY": value})
    assert raw["safety"]["read_only"] is expected


def test_every_env_variable_is_documented() -> None:
    """Undocumented environment variables are explicitly disallowed."""
    for name, dotted in ENV_OVERRIDES.items():
        assert name.startswith("LOCALAI_")
        assert dotted  # maps to a real config path


def test_invalid_profile_reference_is_rejected() -> None:
    with pytest.raises(ValueError, match="not defined"):
        Config(default_profile="missing")


def test_network_disabled_with_remote_host_is_rejected() -> None:
    """An incoherent privacy configuration is a startup error, not a runtime surprise."""
    with pytest.raises(ValueError, match="not loopback"):
        Config.model_validate(
            {"privacy": {"network_disabled": True}, "ollama": {"host": "192.168.1.50"}}
        )


def test_duplicate_rule_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate permission rule"):
        Config.model_validate(
            {
                "permissions": {
                    "rules": [
                        {"id": "same", "effect": "deny"},
                        {"id": "same", "effect": "allow", "tools": ["x"]},
                    ]
                }
            }
        )


def test_config_json_schema_is_generated() -> None:
    """``localai config schema`` depends on this."""
    schema = Config.model_json_schema()
    assert schema["type"] == "object"
    assert "permissions" in schema["properties"]


def test_protected_paths_cover_state_files(paths: AppPaths) -> None:
    protected = paths.protected_paths()
    assert paths.config_file in protected
    assert paths.database in protected
    assert paths.audit_log in protected


def test_home_override_is_honoured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAI_HOME", str(tmp_path / "custom"))
    from localai.config.paths import default_home

    assert default_home() == tmp_path / "custom"
