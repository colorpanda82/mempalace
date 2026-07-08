"""Workspace-owned CLI extensions for the MemPalace argparse app.

This module is the update-safe extension point for workspace-specific CLI
verbs. All new subcommands live here (never in upstream ``cli.py``) so that
upstream rebases never conflict with local additions.

Design contract:
  * Every handler wraps a protocol-agnostic ``tool_*`` function from
    ``mempalace.mcp_server``. Wrapping ``tool_*`` (rather than calling the bare
    storage backends) inherits their validation, quarantine handling, and file
    locking for free. Never bypass them.
  * The ONLY coupling to upstream is the set of wrapped ``tool_*`` names. The
    ``ws-selftest`` subcommand is the post-rebase canary: it imports
    ``mcp_server`` and asserts every wrapped name still exists, catching an
    upstream rename before a real invocation fails cryptically.
  * Each CLI process is short-lived. Setting ``MEMPALACE_PALACE_PATH`` before
    the first import of ``mcp_server`` binds that module's global ``_config`` to
    the requested palace, matching how the MCP server resolves its palace.

Public surface: ``register(sub)`` and ``maybe_dispatch(args)``.
"""

import json
import os
import sys

# The set of upstream tool_* functions this module depends on. ws-selftest
# checks all of these exist as attributes on mcp_server after an upstream rebase.
WRAPPED_TOOLS = [
    "tool_search",
    "tool_add_drawer",
    "tool_check_duplicate",
    "tool_kg_add",
    "tool_kg_query",
    "tool_kg_invalidate",
    "tool_diary_write",
    "tool_diary_read",
]


# --------------------------------------------------------------------------
# Palace resolution + server binding
# --------------------------------------------------------------------------

def _resolve_palace(args):
    p = getattr(args, "palace", None)
    if p:
        return os.path.abspath(os.path.expanduser(p))
    from mempalace.config import MempalaceConfig
    return MempalaceConfig().palace_path


def _server(args):
    # Each CLI call is a fresh process, so setting the env before the FIRST
    # import of mcp_server binds its module-global _config to this palace.
    os.environ["MEMPALACE_PALACE_PATH"] = _resolve_palace(args)
    from mempalace import mcp_server
    return mcp_server


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def _is_error(result):
    """A tool result is an error when it is a dict with success False or an
    explicit 'error' key (the two failure shapes the tool_* functions use)."""
    return isinstance(result, dict) and (
        result.get("success") is False or "error" in result
    )


def _summary(result):
    """Concise one-line human summary of a tool result dict."""
    if not isinstance(result, dict):
        return str(result)
    if _is_error(result):
        msg = result.get("error") or result.get("reason") or "unknown error"
        return f"error: {msg}"
    if isinstance(result.get("results"), list):
        return f"ok: {len(result['results'])} result(s)"
    if isinstance(result.get("entries"), list):
        return f"ok: {len(result['entries'])} entry(ies)"
    bits = []
    for key in (
        "triple_id", "fact", "drawer_id", "invalidated", "duplicate",
        "count", "written", "topic", "message",
    ):
        if key in result:
            bits.append(f"{key}={result[key]}")
    return "ok" + (": " + ", ".join(bits) if bits else "")


def _emit(result, args):
    """Print result (JSON when --json, else a one-line summary) and return an
    exit code: 0 on success, 1 when the wrapped tool reported an error."""
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_summary(result))
    return 1 if _is_error(result) else 0


# --------------------------------------------------------------------------
# Handlers (one per subcommand)
# --------------------------------------------------------------------------

def _h_kg_add(args):
    m = _server(args)
    result = m.tool_kg_add(
        subject=args.subject,
        predicate=args.predicate,
        object=args.object,
        valid_from=args.valid_from,
        valid_to=args.valid_to,
        source_closet=args.source_closet,
        source_file=args.source_file,
        source_drawer_id=args.source_drawer_id,
    )
    return _emit(result, args)


def _h_kg_query(args):
    m = _server(args)
    result = m.tool_kg_query(
        entity=args.entity,
        as_of=args.as_of,
        direction=args.direction,
    )
    return _emit(result, args)


def _h_kg_invalidate(args):
    m = _server(args)
    result = m.tool_kg_invalidate(
        subject=args.subject,
        predicate=args.predicate,
        object=args.object,
        ended=args.ended,
    )
    return _emit(result, args)


def _h_add_drawer(args):
    m = _server(args)
    result = m.tool_add_drawer(
        wing=args.wing,
        room=args.room,
        content=args.content,
        source_file=args.source_file,
        added_by=args.added_by,
    )
    return _emit(result, args)


def _h_check_duplicate(args):
    m = _server(args)
    result = m.tool_check_duplicate(
        content=args.content,
        threshold=args.threshold,
    )
    return _emit(result, args)


def _h_diary_write(args):
    m = _server(args)
    result = m.tool_diary_write(
        agent_name=args.agent_name,
        entry=args.entry,
        topic=args.topic,
        wing=args.wing,
    )
    return _emit(result, args)


def _h_diary_read(args):
    m = _server(args)
    result = m.tool_diary_read(
        agent_name=args.agent_name,
        last_n=args.last_n,
        wing=args.wing,
    )
    return _emit(result, args)


def _h_search_json(args):
    # This is the parseable-search verb: it ALWAYS emits JSON regardless of
    # the --json flag, so downstream tooling can rely on the output shape.
    m = _server(args)
    result = m.tool_search(
        query=args.query,
        limit=args.limit,
        wing=args.wing,
        room=args.room,
        source_file=args.source_file,
        max_distance=args.max_distance,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if _is_error(result) else 0


def _h_ws_selftest(args):
    # Post-rebase canary: no palace or tool call required. Confirm every wrapped
    # tool_* name still exists on mcp_server; a missing name means an upstream
    # rename broke this module.
    try:
        from mempalace import mcp_server as m
    except Exception as e:  # noqa: BLE001 - report import failure as JSON
        print(json.dumps(
            {"ok": False, "present": [], "missing": list(WRAPPED_TOOLS),
             "import_error": str(e)},
            ensure_ascii=False, indent=2,
        ))
        return 1
    present, missing = [], []
    for name in WRAPPED_TOOLS:
        (present if hasattr(m, name) else missing).append(name)
    print(json.dumps(
        {"ok": not missing, "present": present, "missing": missing},
        ensure_ascii=False, indent=2,
    ))
    return 0 if not missing else 1


# Single source of truth mapping our command names -> handlers.
COMMANDS = {
    "kg-add": _h_kg_add,
    "kg-query": _h_kg_query,
    "kg-invalidate": _h_kg_invalidate,
    "add-drawer": _h_add_drawer,
    "check-duplicate": _h_check_duplicate,
    "diary-write": _h_diary_write,
    "diary-read": _h_diary_read,
    "search-json": _h_search_json,
    "ws-selftest": _h_ws_selftest,
}


# --------------------------------------------------------------------------
# Registration + dispatch
# --------------------------------------------------------------------------

def _common(p):
    """Every workspace subparser gets an optional --palace and --json flag."""
    p.add_argument("--palace", help="Palace path (overrides config default)")
    p.add_argument("--json", action="store_true", help="Emit raw JSON result")
    return p


def register(sub):
    """Register all workspace subcommands on the given _SubParsersAction."""
    # kg-add
    p = _common(sub.add_parser("kg-add", help="Add a knowledge-graph triple"))
    p.add_argument("--subject", required=True)
    p.add_argument("--predicate", required=True)
    p.add_argument("--object", required=True)
    p.add_argument("--valid-from")
    p.add_argument("--valid-to")
    p.add_argument("--source-closet")
    p.add_argument("--source-file")
    p.add_argument("--source-drawer-id")

    # kg-query
    p = _common(sub.add_parser("kg-query", help="Query the knowledge graph"))
    p.add_argument("entity")
    p.add_argument("--as-of")
    p.add_argument("--direction", choices=["outgoing", "incoming", "both"],
                   default="both")

    # kg-invalidate
    p = _common(sub.add_parser("kg-invalidate",
                               help="Invalidate a knowledge-graph triple"))
    p.add_argument("--subject", required=True)
    p.add_argument("--predicate", required=True)
    p.add_argument("--object", required=True)
    p.add_argument("--ended")

    # add-drawer
    p = _common(sub.add_parser("add-drawer", help="Add a drawer to the palace"))
    p.add_argument("--wing", required=True)
    p.add_argument("--room", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--source-file")
    p.add_argument("--added-by", default="cli")

    # check-duplicate
    p = _common(sub.add_parser("check-duplicate",
                               help="Check whether content duplicates a drawer"))
    p.add_argument("--content", required=True)
    p.add_argument("--threshold", type=float, default=0.9)

    # diary-write
    p = _common(sub.add_parser("diary-write", help="Write an agent diary entry"))
    p.add_argument("--agent-name", required=True)
    p.add_argument("--entry", required=True)
    p.add_argument("--topic", default="general")
    p.add_argument("--wing", default="")

    # diary-read
    p = _common(sub.add_parser("diary-read", help="Read agent diary entries"))
    p.add_argument("--agent-name", required=True)
    p.add_argument("--last-n", type=int, default=10)
    p.add_argument("--wing", default="")

    # search-json (always JSON)
    p = _common(sub.add_parser("search-json",
                               help="Search the palace, always emitting JSON"))
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--wing")
    p.add_argument("--room")
    p.add_argument("--source-file")
    p.add_argument("--max-distance", type=float, default=1.5)

    # ws-selftest (no palace/tool call needed)
    _common(sub.add_parser(
        "ws-selftest",
        help="Verify wrapped tool_* names still exist on mcp_server",
    ))


def maybe_dispatch(args):
    """Run the handler for one of OUR commands and return its int exit code.

    Return None when args.command is not one of ours, so cli.py's existing
    dispatch chain continues. Never raises for normal tool-error dicts; catches
    unexpected exceptions and returns 1 after printing to stderr.
    """
    cmd = getattr(args, "command", None)
    if cmd not in COMMANDS:
        return None
    try:
        return COMMANDS[cmd](args)
    except Exception as e:  # noqa: BLE001 - surface unexpected failures as rc=1
        print(f"error: command '{cmd}' failed: {e}", file=sys.stderr)
        return 1
