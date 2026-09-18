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
  * Hub first (v3.9.0+). Local file-backed palaces have ONE process-lifetime
    writer (#2079); when a ``mempalace serve`` hub owns the palace, an in-process
    ``tool_*`` write is refused with "palace is held by PID". ``call_tool`` is
    the single seam every workspace writer goes through: it forwards to a live
    hub as an MCP ``tools/call`` when one is registered for the palace, and
    calls ``tool_*`` in-process otherwise. Reads take the same route so scripts
    never hold a private HNSW copy next to the hub. ``MEMPALACE_HUB_FORWARD=0``
    forces the direct path (upstream's own kill switch, shared).

Public surface: ``register(sub)``, ``maybe_dispatch(args)`` and ``call_tool``.
"""

import asyncio
import inspect
import json
import os
import sys
import contextlib

# The set of upstream tool_* functions this module depends on. ws-selftest
# checks all of these exist as attributes on mcp_server after an upstream rebase.
WRAPPED_TOOLS = [
    "tool_search",
    "tool_add_drawer",
    "tool_check_duplicate",
    "tool_kg_add",
    "tool_kg_query",
    "tool_kg_invalidate",
    "tool_kg_supersede",
    "tool_diary_write",
    "tool_diary_read",
]

# Real stdout, captured at module import — which happens during cli.py's
# subparser setup, BEFORE mcp_server is ever imported. mcp_server is an MCP
# stdio server: at import it redirects file descriptor 1 -> 2 (an OS-level
# dup2) so stray library prints can't corrupt its JSON-RPC channel. A Python
# sys.stdout handle cannot survive that. So we DUPLICATE fd 1 here, before the
# redirect, giving us an independent fd to the process's real stdout; all our
# JSON is written to it via _out() and lands on real stdout regardless of what
# mcp_server does to fd 1.
try:
    _REAL_STDOUT = os.fdopen(os.dup(sys.stdout.fileno()), "w", closefd=True)
except (OSError, ValueError, AttributeError):
    # No real fd (e.g. captured/wrapped stdout in a test harness) — fall back.
    _REAL_STDOUT = sys.stdout


def _out(text):
    """Write one line to the duplicated real stdout, immune to fd-1 redirects."""
    _REAL_STDOUT.write(text + "\n")
    _REAL_STDOUT.flush()


# --------------------------------------------------------------------------
# Palace resolution + server binding
# --------------------------------------------------------------------------

def _default_palace():
    """The palace this process is bound to, read the way mcp_server reads it.

    ``MEMPALACE_PALACE_PATH`` wins over ``config.json``: cli.py sets that env
    var from the global ``--palace`` flag AFTER ``mempalace.config`` was
    imported, and ``MempalaceConfig()`` reports the import-time snapshot. Reading
    the config first therefore resolved the wrong palace for hub discovery
    (measured 2026-09-02: discovery on the config palace, write on the env
    palace, refused by the lease).
    """
    env = os.environ.get("MEMPALACE_PALACE_PATH", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    from mempalace.config import MempalaceConfig
    return MempalaceConfig().palace_path


def _resolve_palace(args):
    p = getattr(args, "palace", None)
    if p:
        return os.path.abspath(os.path.expanduser(p))
    return _default_palace()


def _server(args):
    # Each CLI call is a fresh process, so setting the env before the FIRST
    # import of mcp_server binds its module-global _config to this palace.
    os.environ["MEMPALACE_PALACE_PATH"] = _resolve_palace(args)
    from mempalace import mcp_server
    return mcp_server


@contextlib.contextmanager
def _stdout_to_stderr():
    """Redirect stdout to stderr for the duration of a wrapped call.

    The underlying library emits diagnostics to stdout (embedder init, HNSW
    flush-lag notes, 'Filed drawer:' / 'Diary entry:' lines, model-download
    messages). Without this, those lines contaminate our JSON and break
    ``mempalace <verb> --json | jq``. Only the final ``json.dumps(...)`` — printed
    after this context exits — reaches real stdout, keeping --json pipe-clean.
    """
    old = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = old


def _hub_for(palace_path):
    """(base_url, headers) of a live hub serving ``palace_path``, else None."""
    try:
        from mempalace import hub_client
        return hub_client.discover_hub(palace_path)
    except Exception:  # noqa: BLE001 - discovery is best-effort; direct path remains
        return None


def _unwrap_tool_result(response):
    """Turn a JSON-RPC ``tools/call`` response into the dict ``tool_*`` returns.

    The MCP server serialises every tool result as ``json.dumps(result)`` inside
    ``result.content[0].text``; a JSON-RPC ``error`` (peer-writer refusal, bad
    params) is mapped onto the ``{"success": False, "error": ...}`` shape the
    handlers already understand, so callers see one failure vocabulary.
    """
    if not isinstance(response, dict):
        return {"success": False, "error": "hub returned no JSON-RPC response"}
    if "error" in response:
        err = response["error"] or {}
        msg = err.get("message", "unknown hub error") if isinstance(err, dict) else str(err)
        data = err.get("data") if isinstance(err, dict) else None
        if isinstance(data, dict) and data.get("reason"):
            msg = f"{msg}: {data['reason']}"
        return {"success": False, "error": msg, "hub_error": err}
    result = response.get("result") or {}
    content = result.get("content") or []
    text = ""
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text", "")
            break
    try:
        parsed = json.loads(text) if text else {}
    except (TypeError, ValueError):
        parsed = {"text": text}
    if result.get("isError") and isinstance(parsed, dict) and not _is_error(parsed):
        parsed = {"success": False, "error": text or "tool reported isError"}
    return parsed


def _run_maybe_async(value):
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def call_tool(name, args=None, *, palace_path=None):
    """Call one MemPalace tool by short name, via the hub if one owns the palace.

    ``name`` is the tool without prefix (``"add_drawer"``); it becomes
    ``mempalace_add_drawer`` on the hub and ``tool_add_drawer`` in-process.
    ``args`` are the tool's keyword arguments (identical on both routes: the
    hub dispatches ``tools/call`` arguments straight into the same function).
    Returns the tool's result dict. Never raises for tool-level errors.
    """
    args = dict(args or {})
    if palace_path:
        palace_path = os.path.abspath(os.path.expanduser(palace_path))
    else:
        palace_path = _default_palace()
    hub = _hub_for(palace_path)
    if hub is not None:
        from mempalace import hub_client
        base_url, headers = hub
        # The hub validates argument TYPES, so a None that merely means "not
        # given" (every tool_* kwarg defaults to None) is rejected as
        # "Invalid value for parameter" — measured 2026-09-02 on max_distance,
        # which broke every search-json call in the golden recall run. Omit
        # such keys; in-process the default fills them identically.
        hub_args = {k: v for k, v in args.items() if v is not None}
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": f"mempalace_{name}", "arguments": hub_args},
        }
        try:
            response = hub_client.forward_json_rpc(base_url, headers, request)
        except Exception as e:  # noqa: BLE001 - a dead hub must not be silently retried locally
            # A write that reached the hub may still be executing there; never
            # replay it in-process (same rule as mcp_server._dispatch_stdio_request).
            return {"success": False, "error": f"hub call failed: {e!r}", "hub": base_url}
        out = _unwrap_tool_result(response)
        if isinstance(out, dict):
            out.setdefault("via", "hub")
        return out
    # In-process path only: mcp_server binds its module-global _config from
    # MEMPALACE_PALACE_PATH at FIRST import, so the env must be set before it.
    os.environ["MEMPALACE_PALACE_PATH"] = palace_path
    from mempalace import mcp_server
    fn = getattr(mcp_server, f"tool_{name}")
    return _run_maybe_async(fn(**args))


def _invoke(args, fn_name, **kwargs):
    """Bind the palace and call one tool (hub-first) with stdout isolated to
    stderr so only our JSON reaches stdout."""
    with _stdout_to_stderr():
        return call_tool(fn_name[len("tool_"):], kwargs, palace_path=_resolve_palace(args))


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
        "triple_id", "fact", "superseded", "drawer_id", "invalidated", "duplicate",
        "count", "written", "topic", "message",
    ):
        if key in result:
            bits.append(f"{key}={result[key]}")
    return "ok" + (": " + ", ".join(bits) if bits else "")


def _emit(result, args):
    """Print result (JSON when --json, else a one-line summary) and return an
    exit code: 0 on success, 1 when the wrapped tool reported an error."""
    if getattr(args, "json", False):
        _out(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _out(_summary(result))
    return 1 if _is_error(result) else 0


# --------------------------------------------------------------------------
# Handlers (one per subcommand)
# --------------------------------------------------------------------------

def _h_kg_add(args):
    result = _invoke(
        args, "tool_kg_add",
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
    result = _invoke(
        args, "tool_kg_query",
        entity=args.entity,
        as_of=args.as_of,
        direction=args.direction,
    )
    return _emit(result, args)


def _h_kg_invalidate(args):
    result = _invoke(
        args, "tool_kg_invalidate",
        subject=args.subject,
        predicate=args.predicate,
        object=args.object,
        ended=args.ended,
    )
    return _emit(result, args)


def _h_kg_supersede(args):
    result = _invoke(
        args, "tool_kg_supersede",
        subject=args.subject,
        predicate=args.predicate,
        old_object=args.old_object,
        new_object=args.new_object,
        at=args.at,
    )
    return _emit(result, args)


def _h_add_drawer(args):
    result = _invoke(
        args, "tool_add_drawer",
        wing=args.wing,
        room=args.room,
        content=args.content,
        source_file=args.source_file,
        added_by=args.added_by,
    )
    return _emit(result, args)


def _h_check_duplicate(args):
    result = _invoke(
        args, "tool_check_duplicate",
        content=args.content,
        threshold=args.threshold,
    )
    return _emit(result, args)


def _h_diary_write(args):
    result = _invoke(
        args, "tool_diary_write",
        agent_name=args.agent_name,
        entry=args.entry,
        topic=args.topic,
        wing=args.wing,
    )
    return _emit(result, args)


def _h_diary_read(args):
    result = _invoke(
        args, "tool_diary_read",
        agent_name=args.agent_name,
        last_n=args.last_n,
        wing=args.wing,
    )
    return _emit(result, args)


def _h_search_json(args):
    # This is the parseable-search verb: it ALWAYS emits JSON regardless of
    # the --json flag, so downstream tooling can rely on the output shape.
    result = _invoke(
        args, "tool_search",
        query=args.query,
        limit=args.limit,
        wing=args.wing,
        room=args.room,
        source_file=args.source_file,
        max_distance=args.max_distance,
        since=args.since,
        before=args.before,
    )
    _out(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if _is_error(result) else 0


def _h_ws_selftest(args):
    # Post-rebase canary: no palace or tool call required. Confirm every wrapped
    # tool_* name still exists on mcp_server; a missing name means an upstream
    # rename broke this module.
    try:
        with _stdout_to_stderr():
            from mempalace import mcp_server as m
    except Exception as e:  # noqa: BLE001 - report import failure as JSON
        _out(json.dumps(
            {"ok": False, "present": [], "missing": list(WRAPPED_TOOLS),
             "import_error": str(e)},
            ensure_ascii=False, indent=2,
        ))
        return 1
    present, missing = [], []
    for name in WRAPPED_TOOLS:
        (present if hasattr(m, name) else missing).append(name)
    hub = _hub_for(_resolve_palace(args))
    _out(json.dumps(
        {"ok": not missing, "present": present, "missing": missing,
         "hub": hub[0] if hub else None},
        ensure_ascii=False, indent=2,
    ))
    return 0 if not missing else 1


# Single source of truth mapping our command names -> handlers.
COMMANDS = {
    "kg-add": _h_kg_add,
    "kg-query": _h_kg_query,
    "kg-invalidate": _h_kg_invalidate,
    "kg-supersede": _h_kg_supersede,
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
    """Every workspace subparser gets an optional --palace and --json flag.

    ``--palace`` uses ``SUPPRESS`` as its default: argparse applies a
    subparser's defaults AFTER the parent parsed its flags, so a plain ``None``
    default silently overwrote the global ``mempalace --palace X <verb>`` form
    (measured 2026-09-02). With SUPPRESS the verb-level flag only sets
    ``args.palace`` when actually given, and cli.py's global flag survives.
    """
    import argparse
    p.add_argument("--palace", default=argparse.SUPPRESS,
                   help="Palace path (overrides config default)")
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

    # kg-supersede: close (S,P,old) and open (S,P,new) at one instant. The
    # named path for single-valued predicates (version, deployed_on) since
    # v3.9.0; replaces the kg-invalidate + kg-add pair.
    p = _common(sub.add_parser("kg-supersede",
                               help="Atomically replace a single-valued fact"))
    p.add_argument("--subject", required=True)
    p.add_argument("--predicate", required=True)
    p.add_argument("--old-object", required=True)
    p.add_argument("--new-object", required=True)
    p.add_argument("--at", help="Boundary instant (YYYY-MM-DD or ...THH:MM:SSZ); default now")

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
    # None, not 1.5: since v3.9.0 tool_search treats ANY explicit max_distance as
    # a caller-set bound, which switches off the union/fusion-depth override.
    p.add_argument("--max-distance", type=float, default=None)
    # Date window on filed_at, [since, before). Drawers without filed_at are
    # excluded while a bound is set (v3.7.0 #2000).
    p.add_argument("--since", help="ISO date/datetime, inclusive")
    p.add_argument("--before", help="ISO date/datetime, exclusive")

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
