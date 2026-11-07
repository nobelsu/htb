"""``python -m flowmap record|map|heal|show``.

record   open Playwright's recorder on a page, do the action once, and save it as a flow
         (or ``--from-script`` to convert a codegen script you already have)
map      replay a site's flows, infer each action, and save the card to the registry
heal     re-map one action now, through the runtime's healing (pre-check, validation, new version)
show     the active card's actions and the latest heal timings

Paths come from the same environment as the gateway: REGISTRY_DB (./runtime.db), FLOWS_DIR (./flows),
CAPTURES_DIR (./captures), SESSION_KEY for signed-in sites, ANTHROPIC_MODEL for labels.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flowmap.flows import FlowError, flow_path, from_codegen, list_flows, load_flow, save_flow

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _pairs(items: list[str] | None, what: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise SystemExit(f"{what} takes name=value, got {item!r}")
        out.setdefault(name, []).append(value)
    return out


def _is_local(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in LOCAL_HOSTS or host.endswith((".test", ".localhost"))


# ---------- record ----------


def cmd_record(args: argparse.Namespace) -> int:
    if args.from_script:
        source = Path(args.from_script).read_text()
    else:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "recording.py"
            print("Do the action once in the browser that opens, then close it.", file=sys.stderr)
            cmd = [
                sys.executable,
                "-m",
                "playwright",
                "codegen",
                "--target",
                "python",
                "-o",
                str(script),
                args.url,
            ]
            if args.profile_state:
                cmd[4:4] = ["--load-storage", args.profile_state]
            subprocess.run(cmd, check=False)
            if not script.exists():
                print("No recording was saved.", file=sys.stderr)
                return 1
            source = script.read_text()
    inputs = {k: v[0] for k, v in _pairs(args.input, "--input").items()}
    try:
        flow = from_codegen(
            source,
            action=args.action,
            inputs=inputs,
            examples=_pairs(args.example, "--example"),
            no_marker=set(args.no_marker or []),
            domain=args.domain,
            description=args.description,
            auth_provider=args.auth,
            results_selector=args.results,
            next_page=args.next,
        )
    except FlowError as e:
        print(f"Couldn't make a flow: {e}", file=sys.stderr)
        return 1
    if args.write:
        flow = flow.model_copy(update={"kind": "write"})
    path = save_flow(flow, Path(args.flows))
    print(f"Saved {path} ({len(flow.steps)} steps; inputs: {', '.join(flow.inputs) or 'none'})")
    return 0


# ---------- shared runtime pieces ----------


def _runtime(db: str) -> tuple[Any, Any, Any]:
    """(registry, sessions, make_executor) built like the gateway's."""
    from runtime.confirm import Confirmer
    from runtime.executor import Executor
    from runtime.registry import Registry
    from runtime.session_store import ChainedSessionStore, EncryptedSessionStore
    from runtime.sessions import DevEnvSessionStore
    from runtime.tiers import HttpTier, ImpersonateTier

    registry = Registry(db)
    stores: list[Any] = []
    if os.environ.get("SESSION_KEY"):
        stores.append(EncryptedSessionStore(db, os.environ["SESSION_KEY"]))
    stores.append(DevEnvSessionStore(os.environ.get("DEV_SESSION_COOKIES")))
    sessions = ChainedSessionStore(*stores) if len(stores) > 1 else stores[0]
    confirm_secret = os.environ.get("CONFIRMATION_SECRET") or "flowmap-local-replays-only"

    def make_executor(tier: Any = None) -> Executor:
        clients: dict[str, Any] = {"http": HttpTier(), "impersonate": ImpersonateTier()}
        return Executor(
            sessions, Confirmer(confirm_secret), tiers=clients if tier is None else {tier: clients[tier]}
        )

    return registry, sessions, make_executor


def _confirm_write(flow: Any, yes: bool) -> bool:
    first = next((s.url for s in flow.steps if s.op == "goto"), "")
    if flow.kind != "write" or yes or _is_local(first):
        return True
    answer = input(
        f"{flow.action} is a write and will really run several times on {flow.domain}. Continue? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes")


# ---------- map ----------


async def _map(args: argparse.Namespace) -> int:
    from flowmap.assemble import AssembleError, map_action
    from flowmap.capture import capture_flow, load_runs
    from flowmap.infer import InferError
    from flowmap.minimise import executor_replayer

    flows_dir, captures = Path(args.flows), Path(args.captures)
    paths = (
        [flow_path(flows_dir, args.domain, a) for a in args.actions]
        if args.actions
        else list(list_flows(flows_dir, args.domain))
    )
    if not paths:
        print(f"No flows for {args.domain} under {flows_dir}", file=sys.stderr)
        return 1
    registry, _sessions, make_executor = _runtime(args.db)
    stored = registry.get(args.domain)
    existing = stored.card.to_json_dict() if stored else None
    replay = executor_replayer(make_executor)
    failures = 0
    for path in paths:
        try:
            flow = load_flow(path)
        except FlowError as e:
            print(f"{path}: {e}", file=sys.stderr)
            failures += 1
            continue
        if not _confirm_write(flow, args.yes):
            print(f"{flow.action}: skipped")
            continue
        if args.reuse_captures and (runs := load_runs(captures, flow.domain, flow.action)):
            print(f"{flow.action}: using capture {runs[0].session}")
        else:
            print(f"{flow.action}: capturing ({'headed' if args.headed else 'headless'})")
            runs = await capture_flow(
                flow,
                root=captures,
                headless=not args.headed,
                profile_dir=Path(args.profile) / flow.domain if args.profile else None,
                pause_s=args.pause,
            )
        try:
            mapped = await map_action(
                flow,
                runs,
                existing=existing,
                replay=replay,
                registry=None if args.dry_run else registry,
                use_llm=False if args.no_llm else None,
                pause_s=args.pause,
                note=f"flowmap map {flow.action}",
            )
        except (InferError, AssembleError) as e:
            print(f"{flow.action}: failed: {e}", file=sys.stderr)
            for reason in e.reasons:
                print(f"  - {reason}", file=sys.stderr)
            failures += 1
            continue
        existing = mapped.final.card
        report = mapped.report()
        print(json.dumps(report, indent=2))
    if args.dry_run and existing is not None:
        print(json.dumps(existing, indent=2))
    return 1 if failures else 0


# ---------- heal ----------


async def _heal(args: argparse.Namespace) -> int:
    from flowmap.heal import FlowRemapper
    from flowmap.minimise import executor_replayer
    from runtime.healing import HealConfig, HealManager
    from runtime.state import StateStore

    registry, sessions, make_executor = _runtime(args.db)
    if registry.get(args.domain) is None:
        print(f"No card for {args.domain} in {args.db}", file=sys.stderr)
        return 1
    state = StateStore(args.db)
    remapper = FlowRemapper(
        args.flows,
        args.captures,
        sessions=sessions,
        headless=not args.headed,
        pause_s=args.pause,
        replay=executor_replayer(make_executor),
    )
    healer = HealManager(registry, state, make_executor(), remapper, HealConfig(cooldown_s=0))
    heal = healer.page_changed(args.domain, args.action, "manual heal (flowmap heal)", "default")
    assert heal is not None
    await healer.drain()
    job = state.get_job(heal.job_id)
    assert job is not None
    print(
        json.dumps(
            {"job": job.id, "status": job.status, "result": job.result, "new_version": job.new_version},
            indent=2,
        )
    )
    for event in state.events(job.id):
        data = event["data"]
        line = f"{event['event']}: {data.get('ms', '')} ms"
        print(line)
        for action, detail in (data.get("details") or {}).items():
            print(
                f"  {action}: capture {detail['capture_ms']} ms, infer {detail['infer_ms']} ms, "
                f"total {detail['total_ms']} ms, confidence {detail['confidence']}"
            )
            for diff in detail["diff"]:
                print(f"    {diff}")
    return 0 if job.status == "succeeded" else 1


# ---------- show ----------


def cmd_show(args: argparse.Namespace) -> int:
    from runtime.registry import Registry
    from runtime.state import StateStore

    registry = Registry(args.db)
    stored = registry.get(args.domain)
    if stored is None:
        print(f"No card for {args.domain} in {args.db}", file=sys.stderr)
        return 1
    card = stored.card
    if args.json:
        print(json.dumps(card.to_json_dict(), indent=2))
        return 0
    print(f"{card.domain} v{stored.version} ({card.provenance.source}, {card.provenance.mapper_version})")
    for action in card.actions:
        if args.action and action.name != args.action:
            continue
        step = action.steps[-1].request
        flags = " confirm" if action.requires_confirmation else ""
        print(f"\n{action.name}  {action.kind}{flags}  tier {action.tier or card.defaults.tier}")
        print(f"  {action.description}")
        print(f"  {step.method} {step.url}")
        for name, p in action.params.items():
            print(
                f"  param {name}: {p.type}{' required' if p.required else ''}  {p.description or ''}".rstrip()
            )
        for name, path in action.output.fields.items():
            print(f"  field {name}: {path}")
    events = [
        e for e in StateStore(args.db).events(domain=args.domain) if e["event"] in ("remap_done", "heal_done")
    ]
    if events:
        print("\nheal timings")
        for e in events[-10:]:
            print(f"  {e['at']} {e['job_id']} {e['event']} {e['data'].get('ms')} ms")
    return 0


# ---------- parser ----------


def parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(prog="python -m flowmap", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def paths(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default=env("REGISTRY_DB", "./runtime.db"))
        sp.add_argument("--flows", default=env("FLOWS_DIR", "./flows"))
        sp.add_argument("--captures", default=env("CAPTURES_DIR", "./captures"))

    r = sub.add_parser("record", help="record one action as a flow")
    r.add_argument("url", nargs="?", default="")
    r.add_argument("--action", required=True, help="snake_case action name, e.g. search_companies")
    r.add_argument("--input", action="append", help="name=value you typed while recording (repeatable)")
    r.add_argument("--example", action="append", help="name=value, another real value for an input")
    r.add_argument("--no-marker", action="append", help="an input that can't take a marker")
    r.add_argument("--results", help="CSS selector of each result's main text, e.g. '.result h3'")
    r.add_argument("--next", help="CSS selector of the next-page link")
    r.add_argument("--auth", help="connect provider id, for actions that need a login")
    r.add_argument("--domain", help="the card's domain (default: the first page's host)")
    r.add_argument("--description", help="what the action does, in your words")
    r.add_argument("--write", action="store_true", help="the action changes something (a write)")
    r.add_argument("--from-script", help="convert this codegen script instead of opening the recorder")
    r.add_argument("--profile-state", help="Playwright storage state to record signed in")
    r.add_argument("--flows", default=env("FLOWS_DIR", "./flows"))

    m = sub.add_parser("map", help="capture and map a site's flows into the registry")
    m.add_argument("domain")
    m.add_argument("actions", nargs="*")
    paths(m)
    m.add_argument("--headed", action="store_true")
    m.add_argument("--profile", help="folder for persistent browser profiles (one per domain)")
    m.add_argument("--pause", type=float, default=2.0, help="seconds between requests to the site")
    m.add_argument("--reuse-captures", action="store_true", help="map the latest saved capture again")
    m.add_argument("--no-llm", action="store_true", help="name things by rules only")
    m.add_argument("--dry-run", action="store_true", help="print the card instead of saving it")
    m.add_argument("--yes", action="store_true", help="don't ask before replaying a write on a real site")

    h = sub.add_parser("heal", help="re-map one action now")
    h.add_argument("domain")
    h.add_argument("action")
    paths(h)
    h.add_argument("--headed", action="store_true")
    h.add_argument("--pause", type=float, default=2.0)

    s = sub.add_parser("show", help="show a site's active card and heal timings")
    s.add_argument("domain")
    s.add_argument("action", nargs="?")
    s.add_argument("--db", default=env("REGISTRY_DB", "./runtime.db"))
    s.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    args = parser().parse_args(argv)
    if args.cmd == "record":
        if not args.from_script and not args.url:
            raise SystemExit("record needs a url (or --from-script)")
        return cmd_record(args)
    if args.cmd == "map":
        return asyncio.run(_map(args))
    if args.cmd == "heal":
        return asyncio.run(_heal(args))
    return cmd_show(args)
