"""Persistence for conversations and messages.

Messages are written as they are produced rather than at the end of a turn, so a
crash mid-generation loses at most the partial assistant message -- everything
before it is already durable. This is the "preserve conversations during crashes"
requirement, and it is why the agent loop calls :meth:`ConversationStore.add_message`
incrementally instead of batching.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from localai.domain.messages import Message, Role, ToolCall, new_id
from localai.storage.db import Database


@dataclass(slots=True)
class ConversationRecord:
    """Conversation metadata, without its messages."""

    id: str
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    workspace: str = ""
    model: str = ""
    system_prompt: str | None = None
    parent_id: str | None = None
    forked_from_seq: int | None = None
    archived: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    message_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace": self.workspace,
            "model": self.model,
            "parent_id": self.parent_id,
            "forked_from_seq": self.forked_from_seq,
            "archived": self.archived,
            "message_count": self.message_count,
        }


def _row_to_conversation(row: Any) -> ConversationRecord:
    return ConversationRecord(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        workspace=row["workspace"],
        model=row["model"],
        system_prompt=row["system_prompt"],
        parent_id=row["parent_id"],
        forked_from_seq=row["forked_from_seq"],
        archived=bool(row["archived"]),
        metadata=json.loads(row["metadata_json"]),
        message_count=row["message_count"] if "message_count" in row.keys() else 0,
    )


def _row_to_message(row: Any) -> Message:
    return Message(
        id=row["id"],
        role=Role(row["role"]),
        content=row["content"],
        thinking=row["thinking"],
        tool_calls=[
            ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
            for c in json.loads(row["tool_calls_json"])
        ],
        tool_call_id=row["tool_call_id"],
        name=row["tool_name"],
        created_at=row["created_at"],
        metadata=json.loads(row["metadata_json"]),
    )


class ConversationStore:
    """CRUD, search, fork and export for conversations."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- conversations --------------------------------------------------------

    def create(
        self,
        *,
        title: str = "",
        workspace: str = "",
        model: str = "",
        system_prompt: str | None = None,
        parent_id: str | None = None,
        forked_from_seq: int | None = None,
    ) -> ConversationRecord:
        record = ConversationRecord(
            id=new_id("conv"),
            title=title,
            workspace=workspace,
            model=model,
            system_prompt=system_prompt,
            parent_id=parent_id,
            forked_from_seq=forked_from_seq,
        )
        self.db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, workspace, model,"
            " system_prompt, parent_id, forked_from_seq, archived, metadata_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,0,'{}')",
            (
                record.id,
                record.title,
                record.created_at,
                record.updated_at,
                record.workspace,
                record.model,
                record.system_prompt,
                record.parent_id,
                record.forked_from_seq,
            ),
        )
        return record

    def get(self, conversation_id: str) -> ConversationRecord | None:
        row = self.db.query_one(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)"
            " AS message_count FROM conversations c WHERE c.id = ?",
            (conversation_id,),
        )
        return _row_to_conversation(row) if row else None

    def recent(
        self, *, limit: int = 50, workspace: str | None = None, include_archived: bool = False
    ) -> list[ConversationRecord]:
        clauses, params = [], []
        if workspace:
            clauses.append("c.workspace = ?")
            params.append(workspace)
        if not include_archived:
            clauses.append("c.archived = 0")
        # `clauses` holds only the two hard-coded literals above -- no caller-supplied
        # text ever reaches it -- and every value is bound as a parameter below.
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)"  # noqa: S608
            f" AS message_count FROM conversations c {where} ORDER BY c.updated_at DESC LIMIT ?",
            (*params, limit),
        )
        return [_row_to_conversation(r) for r in rows]

    def rename(self, conversation_id: str, title: str) -> None:
        self.db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, time.time(), conversation_id),
        )

    def set_model(self, conversation_id: str, model: str) -> None:
        self.db.execute(
            "UPDATE conversations SET model = ?, updated_at = ? WHERE id = ?",
            (model, time.time(), conversation_id),
        )

    def delete(self, conversation_id: str) -> None:
        """Delete a conversation and its messages (cascade)."""
        self.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    # -- messages -------------------------------------------------------------

    def add_message(self, conversation_id: str, message: Message) -> int:
        """Append a message and return its sequence number.

        Sequence numbers come from ``MAX(seq) + 1`` inside the same statement, so
        two concurrent writers cannot produce a duplicate: the UNIQUE constraint on
        ``(conversation_id, seq)`` would reject the loser.
        """
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            seq = int(row["next"])
            connection.execute(
                "INSERT INTO messages (id, conversation_id, seq, role, content, thinking,"
                " tool_calls_json, tool_call_id, tool_name, created_at, token_estimate,"
                " metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message.id,
                    conversation_id,
                    seq,
                    message.role.value,
                    message.content,
                    message.thinking,
                    json.dumps([c.to_dict() for c in message.tool_calls]),
                    message.tool_call_id,
                    message.name,
                    message.created_at,
                    message.approx_tokens(),
                    json.dumps(message.metadata, default=str),
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (time.time(), conversation_id),
            )
        return seq

    def messages(self, conversation_id: str, *, up_to_seq: int | None = None) -> list[Message]:
        sql = "SELECT * FROM messages WHERE conversation_id = ?"
        params: list[Any] = [conversation_id]
        if up_to_seq is not None:
            sql += " AND seq <= ?"
            params.append(up_to_seq)
        return [_row_to_message(r) for r in self.db.query(sql + " ORDER BY seq", params)]

    def update_message(self, message: Message) -> None:
        """Rewrite a message in place. Used to finalise a streamed assistant message."""
        self.db.execute(
            "UPDATE messages SET content = ?, thinking = ?, tool_calls_json = ?,"
            " token_estimate = ?, metadata_json = ? WHERE id = ?",
            (
                message.content,
                message.thinking,
                json.dumps([c.to_dict() for c in message.tool_calls]),
                message.approx_tokens(),
                json.dumps(message.metadata, default=str),
                message.id,
            ),
        )

    # -- fork -----------------------------------------------------------------

    def fork(self, conversation_id: str, *, at_seq: int, title: str = "") -> ConversationRecord:
        """Create a new conversation containing messages 0..``at_seq`` inclusive.

        The original is untouched, which is the point: forking is for exploring an
        alternative continuation without losing the branch you came from.
        """
        source = self.get(conversation_id)
        if source is None:
            raise KeyError(conversation_id)

        forked = self.create(
            title=title or f"{source.title or 'conversation'} (fork @{at_seq})",
            workspace=source.workspace,
            model=source.model,
            system_prompt=source.system_prompt,
            parent_id=source.id,
            forked_from_seq=at_seq,
        )
        for message in self.messages(conversation_id, up_to_seq=at_seq):
            message.id = new_id("msg")  # new identity; content is copied
            self.add_message(forked.id, message)
        return forked

    # -- search ---------------------------------------------------------------

    def search(self, query: str, *, limit: int = 40) -> list[dict[str, Any]]:
        """Search message text, preferring FTS5 and falling back to LIKE.

        The fallback is not a silent downgrade: :attr:`Database.fts_available` is
        reported by ``localai doctor`` so the user knows which engine is in use.
        """
        if self.db.fts_available:
            rows = self.db.query(
                "SELECT m.id, m.conversation_id, m.seq, m.role, m.created_at,"
                " c.title, snippet(messages_fts, 0, '[', ']', '...', 12) AS snippet"
                " FROM messages_fts f"
                " JOIN messages m ON m.id = f.message_id"
                " JOIN conversations c ON c.id = m.conversation_id"
                " WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            )
        else:
            rows = self.db.query(
                "SELECT m.id, m.conversation_id, m.seq, m.role, m.created_at, c.title,"
                " substr(m.content, 1, 200) AS snippet"
                " FROM messages m JOIN conversations c ON c.id = m.conversation_id"
                " WHERE m.content LIKE ? ORDER BY m.created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            )
        return [dict(r) for r in rows]

    # -- export ---------------------------------------------------------------

    def export_json(self, conversation_id: str) -> dict[str, Any]:
        record = self.get(conversation_id)
        if record is None:
            raise KeyError(conversation_id)
        return {
            "schema": "localai.conversation/1",
            "conversation": record.to_dict(),
            "system_prompt": record.system_prompt,
            "messages": [
                {
                    "role": m.role.value,
                    "content": m.content,
                    "thinking": m.thinking,
                    "tool_calls": [c.to_dict() for c in m.tool_calls],
                    "tool_call_id": m.tool_call_id,
                    "tool_name": m.name,
                    "created_at": m.created_at,
                    "metadata": m.metadata,
                }
                for m in self.messages(conversation_id)
            ],
        }

    def export_markdown(self, conversation_id: str, *, include_thinking: bool = False) -> str:
        """Render as Markdown suitable for pasting into notes or a bug report."""
        record = self.get(conversation_id)
        if record is None:
            raise KeyError(conversation_id)

        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.created_at))
        lines = [
            f"# {record.title or 'Conversation'}",
            "",
            f"- **Model:** {record.model or 'unknown'}",
            f"- **Started:** {created}",
            f"- **Workspace:** {record.workspace or '(none)'}",
            f"- **ID:** `{record.id}`",
            "",
        ]
        if record.system_prompt:
            lines += ["## System prompt", "", "```", record.system_prompt, "```", ""]

        headings = {
            Role.USER: "## You",
            Role.ASSISTANT: "## Assistant",
            Role.TOOL: "### Tool result",
            Role.SYSTEM: "### System",
        }
        for message in self.messages(conversation_id):
            lines.append(headings.get(message.role, f"### {message.role.value}"))
            lines.append("")
            if message.thinking and include_thinking:
                lines += [
                    "<details><summary>Reasoning</summary>",
                    "",
                    message.thinking,
                    "",
                    "</details>",
                    "",
                ]
            if message.tool_calls:
                for call in message.tool_calls:
                    lines += [
                        f"**Tool call:** `{call.name}`",
                        "",
                        "```json",
                        json.dumps(call.arguments, indent=2),
                        "```",
                        "",
                    ]
            if message.content:
                if message.role is Role.TOOL:
                    lines += [f"`{message.name or 'tool'}`:", "", "```", message.content, "```", ""]
                else:
                    lines += [message.content, ""]
        return "\n".join(lines)

    def export_to_file(self, conversation_id: str, destination: Path, *, fmt: str = "md") -> Path:
        """Write an export to disk, creating parent directories as needed."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            destination.write_text(
                json.dumps(self.export_json(conversation_id), indent=2, default=str),
                encoding="utf-8",
            )
        else:
            destination.write_text(self.export_markdown(conversation_id), encoding="utf-8")
        return destination
