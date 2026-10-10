"""Provider-neutral WebChat tool catalog and deny-first policy gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from zf.web.agent_runtime_db import digest_json
from zf.web.agent_turn_store import AgentTurnStore


TOOL_CLASSES = frozenset({"read", "workspace", "controlled", "external"})
TOOL_POLICIES = frozenset({"allow", "deny", "ask"})


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    tool_class: str
    description: str
    aliases: tuple[str, ...] = ()
    default_policy: str = "deny"
    durable_background: bool = False
    controlled_action: str = ""

    def __post_init__(self) -> None:
        if self.tool_class not in TOOL_CLASSES:
            raise ValueError(f"invalid tool class {self.tool_class!r}")
        if self.default_policy not in TOOL_POLICIES:
            raise ValueError(f"invalid tool policy {self.default_policy!r}")


DEFAULT_TOOLS = (
    ToolDefinition("task.read", "read", "Read task projection", ("read_task",), "allow"),
    ToolDefinition("workflow.read", "read", "Read workflow projection", ("read_workflow",), "allow"),
    ToolDefinition("trace.search", "read", "Search trace and event projection", ("search_trace",), "allow"),
    ToolDefinition("artifact.read", "read", "Read an authorized artifact", ("read_artifact",), "allow"),
    ToolDefinition("workspace.read", "workspace", "Read files in the authorized workspace", ("read_file",), "allow"),
    ToolDefinition("workspace.edit", "workspace", "Edit files in the authorized workspace", ("edit_file",), "ask"),
    ToolDefinition("workspace.shell", "workspace", "Run a bounded workspace command", ("shell", "bash"), "ask"),
    ToolDefinition("workspace.test", "workspace", "Run project verification", ("test",), "allow"),
    ToolDefinition("task.create", "controlled", "Propose a canonical task", ("create_task",), "ask", controlled_action="create-task"),
    ToolDefinition("workflow.start", "controlled", "Propose a workflow start", ("start_workflow",), "ask", controlled_action="workflow-start"),
    ToolDefinition("run.control", "controlled", "Request a sanctioned run control", ("control_run",), "ask", controlled_action="run-control"),
    ToolDefinition("web.search", "external", "Search public Web sources", ("search_web",), "allow"),
    ToolDefinition("mcp.call", "external", "Invoke a host-owned MCP tool", ("call_mcp",), "ask"),
)


@dataclass(frozen=True)
class ToolDecision:
    decision: str
    canonical_name: str
    reason: str
    args_digest: str
    interaction: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "webchat-tool-decision.v1",
            "decision": self.decision,
            "canonical_name": self.canonical_name,
            "reason": self.reason,
            "args_digest": self.args_digest,
            "interaction": self.interaction,
        }


class AgentToolCatalog:
    def __init__(self, tools: Iterable[ToolDefinition] = DEFAULT_TOOLS) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._aliases: dict[str, str] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool {tool.name!r}")
            self._tools[tool.name] = tool
            for alias in (tool.name, *tool.aliases):
                normalized = self._normalize(alias)
                existing = self._aliases.get(normalized)
                if existing and existing != tool.name:
                    raise ValueError(f"duplicate tool alias {alias!r}")
                self._aliases[normalized] = tool.name

    @staticmethod
    def _normalize(value: str) -> str:
        return value.strip().lower().replace("-", "_").replace(".", "_")

    def resolve(self, name: str) -> ToolDefinition:
        canonical = self._aliases.get(self._normalize(name))
        if not canonical:
            raise KeyError(name)
        return self._tools[canonical]

    def projection(
        self,
        *,
        surface: str,
        unattended: bool = False,
        selected_tools: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected: set[str] | None = None
        if selected_tools is not None:
            selected = set()
            for name in selected_tools:
                try:
                    selected.add(self.resolve(str(name)).name)
                except KeyError as exc:
                    raise ValueError(f"unknown selected tool {name!r}") from exc
        items = []
        for tool in sorted(self._tools.values(), key=lambda item: item.name):
            if selected is not None and tool.name not in selected:
                continue
            item = asdict(tool)
            item["aliases"] = list(tool.aliases)
            item["effective_policy"] = self._effective_policy(
                tool,
                surface=surface,
                unattended=unattended,
                overrides={},
            )
            items.append(item)
        return {
            "schema_version": "webchat-tool-catalog.v1",
            "surface": surface,
            "unattended": unattended,
            "items": items,
            "digest": digest_json(items),
            "credentials_exposed": False,
            "provider_native_interception": False,
            "host_controlled_only": True,
        }

    def evaluate(
        self,
        *,
        state_dir: Any,
        turn_id: str,
        attempt_id: str,
        name: str,
        args: Mapping[str, Any],
        workspace: str,
        surface: str,
        unattended: bool = False,
        overrides: Mapping[str, str] | None = None,
    ) -> ToolDecision:
        try:
            tool = self.resolve(name)
        except KeyError:
            return ToolDecision("deny", "", "unknown tool", digest_json(args))
        policy = self._effective_policy(
            tool,
            surface=surface,
            unattended=unattended,
            overrides=overrides or {},
        )
        args_digest = digest_json(args)
        if policy == "deny":
            return ToolDecision("deny", tool.name, "tool denied by effective policy", args_digest)
        if policy == "allow":
            return ToolDecision("allow", tool.name, "tool allowed by effective policy", args_digest)
        currentness = {
            "tool": tool.name,
            "args_digest": args_digest,
            "workspace": workspace,
            "attempt_id": attempt_id,
            "surface": surface,
        }
        interaction, _ = AgentTurnStore(state_dir).request_interaction(
            turn_id=turn_id,
            attempt_id=attempt_id,
            kind="tool_approval",
            request={
                "tool": tool.name,
                "description": tool.description,
                "args_digest": args_digest,
                "workspace": workspace,
                "choices": ["approve", "deny"],
            },
            currentness=currentness,
        )
        return ToolDecision("ask", tool.name, "owner approval required", args_digest, interaction)

    @staticmethod
    def _effective_policy(
        tool: ToolDefinition,
        *,
        surface: str,
        unattended: bool,
        overrides: Mapping[str, str],
    ) -> str:
        explicit = {
            str(overrides.get(tool.name) or "").strip(),
            str(overrides.get(tool.tool_class) or "").strip(),
        } - {""}
        invalid = explicit - TOOL_POLICIES
        if invalid:
            return "deny"
        if "deny" in explicit:
            return "deny"
        if "ask" in explicit:
            return "ask"
        if "allow" in explicit:
            return "allow"
        if surface == "worker" and unattended and tool.tool_class == "workspace":
            return "allow" if tool.default_policy != "deny" else "deny"
        return tool.default_policy


def mcp_controlled_intent(
    *,
    server: str,
    tool: str,
    args: Mapping[str, Any],
    credential_ref: str = "",
) -> dict[str, Any]:
    """Build a secret-free MCP intent; execution remains host controlled."""

    forbidden = {"api_key", "apikey", "token", "password", "secret", "credential"}

    def leaked_fields(value: Any, *, prefix: str = "args") -> list[str]:
        found: list[str] = []
        if isinstance(value, Mapping):
            for key, child in value.items():
                path = f"{prefix}.{key}"
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in forbidden or normalized.endswith("_token"):
                    found.append(path)
                found.extend(leaked_fields(child, prefix=path))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                found.extend(leaked_fields(child, prefix=f"{prefix}[{index}]"))
        return found

    leaked = leaked_fields(args)
    if leaked:
        raise ValueError(f"MCP args contain credential fields: {', '.join(leaked)}")
    if not server.strip() or not tool.strip():
        raise ValueError("MCP server and tool are required")
    return {
        "schema_version": "webchat-mcp-intent.v1",
        "server": server.strip(),
        "tool": tool.strip(),
        "args": dict(args),
        "args_digest": digest_json(args),
        "host_credential_configured": bool(credential_ref.strip()),
        "credentials_exposed": False,
        "effect": "controlled_intent_only",
    }


def approval_currentness(
    *,
    tool: str,
    args: Mapping[str, Any],
    workspace: str,
    attempt_id: str,
    surface: str,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "args_digest": digest_json(args),
        "workspace": workspace,
        "attempt_id": attempt_id,
        "surface": surface,
    }


__all__ = [
    "AgentToolCatalog",
    "DEFAULT_TOOLS",
    "TOOL_CLASSES",
    "TOOL_POLICIES",
    "ToolDecision",
    "ToolDefinition",
    "approval_currentness",
    "mcp_controlled_intent",
]
