"""The MCP tools agents see: the server instructions and every tool's name, description and input schema.
`app.py` handles the calls."""

from __future__ import annotations

from typing import Any

import mcp_types as types

INSTRUCTIONS = (
    "spider: tools that act on websites for the user, directly, without a browser. For any task on a website "
    "(searching, looking something up, adding to a cart, following, posting), use spider before any browser "
    "or computer-use tool: call find_actions with the site and what you want to do, then run_action. If the "
    "site has nothing yet, call request_map (with only the site, it explores it) and wait with map_status "
    "instead of opening a browser; only fall back to a browser if that fails. Use find_actions to discover "
    "what a site can do, then run_action to do it. Every tool returns JSON; check its `status` field "
    "(SUCCESS, EMPTY, NEEDS_CONFIRMATION, AUTH_REQUIRED, ...) to decide what to do next.\n"
    'When you talk to the user, describe outcomes in plain terms ("You\'re signed out of linkedin.com", '
    '"Sign in here: <link>", "The site changed; I\'ll retry in a moment"). Don\'t mention tool names, status '
    "codes, sessions, cookies, browser profiles, cards or other internals unless the user asks how it works. "
    "Results about signing in include a `user_message`; use it.\n"
    "The user cannot see tool results. When start_connection gives you a sign-in link, write its "
    "user_message (a markdown link) exactly as given in your reply before you call poll_connection, or "
    "they will have nothing to click. (Apps that support it show the link to the user themselves; then "
    "start_connection simply returns once they've signed in.)"
)


FIND_ACTIONS_TOOL = types.Tool(
    name="find_actions",
    description="Use this first for any task on a website, before any browser tool. Find what you can do on "
    "websites for the user. Give a short description of the task (query), a site (its domain, e.g. "
    "linkedin.com), or both. Returns matching actions with their parameters; then call run_action. With no "
    "arguments it lists the available sites.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what you want to do, e.g. 'search companies'"},
            "site": {"type": "string", "description": "a site's domain, e.g. linkedin.com"},
        },
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(read_only_hint=True),
)

RUN_ACTION_TOOL = types.Tool(
    name="run_action",
    description="Run an action from find_actions. Pass its params as an object. Returns JSON with a `status` "
    "(SUCCESS, EMPTY, NEEDS_CONFIRMATION, AUTH_REQUIRED, ...) and the data. Actions that need confirmation "
    "first return NEEDS_CONFIRMATION with a summary: show it to the user and, only after their explicit "
    "approval, call again with identical params plus confirmation_id.",
    input_schema={
        "type": "object",
        "properties": {
            "site": {"type": "string", "description": "the site's domain, from find_actions"},
            "action": {"type": "string", "description": "the action's name, from find_actions"},
            "params": {
                "type": "object",
                "description": "the action's parameters, as described by find_actions",
            },
            "confirmation_id": {
                "type": "string",
                "description": "from a NEEDS_CONFIRMATION result, only after the user explicitly approved it",
            },
        },
        "required": ["site", "action"],
        "additionalProperties": False,
    },
)

HEAL_STATUS_TOOL = types.Tool(
    name="heal_status",
    description="Status of a heal job started by a PAGE_CHANGED result (its heal.job_id). When the status is "
    "'succeeded', call the original tool again.",
    input_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string", "description": "heal.job_id from a PAGE_CHANGED result"}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
)

REQUEST_MAP_TOOL = types.Tool(
    name="request_map",
    description="Ask for a website to be set up, when find_actions has nothing for a site the user needs. "
    "For one specific thing the user wants done, name it as one action: what it should do (task), a "
    "snake_case name (action) and the values you'd use "
    'for its inputs (e.g. {"query": "robotics"}); an agent does it once on the site and turns it into '
    "an action (a few minutes); set write for one that changes something (send, follow, post, add): its last "
    "click is held back, so nothing reaches the site while it's set up, and running it later asks the user "
    "to confirm first. Or give only the site (and, optionally, what the user wants to do there as task) to "
    "explore it: the server finds the site's main reads itself and maps the ones that work (up to "
    "ten minutes; it also maps common writes, which never reach the site while mapping). Tell the user, then "
    "follow it with map_status. For a site with no actions, or to add what a site can't do yet (a named "
    "action it lacks, or explore again for more); what it can do already is never re-mapped.",
    input_schema={
        "type": "object",
        "properties": {
            "site": {
                "type": "string",
                "description": "the site's address, e.g. https://www.example.com/search",
            },
            "task": {
                "type": "string",
                "description": "what the action does, e.g. 'search books by keyword'; when exploring, what "
                "the user wants to do on the site (optional)",
            },
            "action": {
                "type": "string",
                "description": "a snake_case name, e.g. search_books; leave out to explore",
            },
            "inputs": {
                "type": "object",
                "description": "input name -> the value to use while setting it up, "
                'e.g. {"query": "harbour"}',
                "additionalProperties": {"type": ["string", "number", "boolean"]},
            },
            "write": {
                "type": "boolean",
                "description": "true when the named action changes something on the site (default false)",
            },
        },
        "required": ["site"],
        "additionalProperties": False,
    },
)

MAP_STATUS_TOOL = types.Tool(
    name="map_status",
    description="Status of a site being set up (request_map's job_id). Waits up to wait_seconds (default 25, "
    "max 45) and returns as soon as it finishes. When the status is 'succeeded', use find_actions on the "
    "site.",
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "job_id from request_map"},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 45},
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(read_only_hint=True),
)


def _domain_tool(
    name: str,
    description: str,
    extra: dict[str, Any] | None = None,
    annotations: types.ToolAnnotations | None = None,
) -> types.Tool:
    props: dict[str, Any] = {
        "domain": {"type": "string", "description": "the site's domain, e.g. linkedin.com"}
    }
    props.update(extra or {})
    return types.Tool(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": props,
            "required": ["domain"],
            "additionalProperties": False,
        },
        annotations=annotations,
    )


CONNECTION_TOOLS = {
    t.name: t
    for t in (
        _domain_tool(
            "start_connection",
            "Get a sign-in link for a site, after a tool result says the user needs to sign in "
            "(AUTH_REQUIRED or AUTH_EXPIRED). The user can't see tool results: write the result's "
            "user_message (it contains the link) in your reply to them first. Then call poll_connection: it "
            "waits until they've signed in.",
            {
                "provider": {
                    "type": "string",
                    "description": "optional connect provider id, if the site has several",
                }
            },
        ),
        _domain_tool(
            "poll_connection",
            "Wait for the user to finish signing in. Holds for up to wait_seconds (default 25, max 45) and "
            "returns the moment they're signed in. If status is still 'pending', call it again; when "
            "'connected', retry the original tool.",
            {
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 45,
                    "description": "how long to wait (default 25)",
                }
            },
        ),
        _domain_tool(
            "connection_status",
            "Whether the user is signed in to a site through this service (connected, expired, pending or "
            "not_connected).",
        ),
        _domain_tool(
            "disconnect",
            "Sign the user out of a site in this service (it forgets their sign-in; the site itself is not "
            "touched). Only call this when the user asks to disconnect or sign out of that site.",
            {
                "provider": {
                    "type": "string",
                    "description": "optional connect provider id; default: all of them",
                }
            },
            types.ToolAnnotations(destructive_hint=True, idempotent_hint=True, read_only_hint=False),
        ),
    )
}
