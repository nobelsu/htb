"""Streamed data-row answers (React Server Components style), as LinkedIn's web app sends them."""

import json
import re
from types import SimpleNamespace

import pytest
from fixture_site import scripts

from spider.mappers.harmap.infer.load import content_kind, is_row_stream
from spider.mappers.harmap.llm import LLM
from spider.mappers.harmap.pipeline import map_domain
from spider.mappers.harmap.record.capture import CaptureConfig, record

ROWS = '0:["$","h2",null,{"children":"x"}]\n1:I["a.js",[],"A"]\n2:{"name":"Acme"}\n'


def test_streamed_rows_are_recognised_by_shape():
    assert content_kind("application/octet-stream", ROWS) == "stream"
    assert content_kind("text/x-component", ROWS) == "stream"
    assert content_kind("application/octet-stream", "\x89PNG\r\n") == "other"
    assert content_kind("text/plain", "hello\nworld") == "text"
    assert content_kind("application/json", '{"a": 1}') == "json"
    assert not is_row_stream("12:30 meeting\n13:00 lunch")  # looks like rows, but no JSON in them


class Brain:
    """Stands in for the mapper's LLM: the goal tie-break and the output field names; everything else
    fails, so the mapper uses its deterministic fallbacks."""

    def __init__(self):
        self.prompts = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        schema = kw["output_config"]["format"]["schema"]["properties"]
        prompt = kw["messages"][0]["content"]
        lines = [m for m in (re.match(r"^(\d+)\. (.*)$", ln) for ln in prompt.splitlines()) if m]
        if "index" in schema:  # which request performs the task
            payload = {"index": next(int(m[1]) for m in lines if "/api/stream/search" in m[2]), "reason": "-"}
        elif "fields" in schema:  # name the output fields
            picks = {}
            for m in lines:
                vals = m[2]
                name = (
                    "name"
                    if re.search(r"Labs|Systems|Works", vals)
                    else "link"
                    if "/company/" in vals
                    else "followers"
                    if "followers" in vals
                    else None
                )
                if name and name not in picks:
                    picks[name] = int(m[1])
            payload = {"fields": [{"index": i, "name": n} for n, i in picks.items()]}
        else:
            raise RuntimeError("not used in this test")
        self.prompts.append(prompt)
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


@pytest.fixture
def stream_task(chromium, site, tmp_path):
    """m1, m2 and real recordings of the fixture's streamed search. Returns the domain folder."""
    state = tmp_path / "login.state.json"
    site.login_state(state)
    bundle = None
    for run, kw in [
        ("m1", {"markers": {"query": "zqxv8841"}}),
        ("m2", {"markers": {"query": "kjtr5527"}}),
        ("real", {"real_inputs": {"query": "software"}}),
    ]:
        bundle = record(
            CaptureConfig(
                start_url=site.url + "/",
                task="stream_search",
                run=run,
                description="Search companies by keyword",
                state=state,
                out=tmp_path / "captures",
                headless=True,
                script=scripts.STREAM_SEARCH,
                force=True,
                **kw,
            )
        )
    return bundle.parent.parent


def _as_explorer_recording(domain_dir):
    meta_path = domain_dir / "stream_search" / "real" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["driver"] = "agent"  # what the explorer's live run writes
    meta_path.write_text(json.dumps(meta))


@pytest.mark.browser
def test_streamed_search_maps_to_a_working_card(stream_task):
    _as_explorer_recording(stream_task)
    brain = Brain()
    mapped = map_domain(stream_task, llm=LLM(client=brain, enabled=True), live=True)
    assert mapped.validation.ok, mapped.validation.errors
    [action] = mapped.card["actions"]
    # the tie-break got both calls with their shape and size, and picked the search over the decoy
    tie = next(p for p in brain.prompts if "/api/stream/log" in p)
    assert "streamed answer" in tie and "kB" in tie
    goal = action["steps"][-1]
    assert goal["request"]["url"].endswith("/api/stream/search")
    # a POST, but the explorer said read: no confirmation needed
    assert (action["kind"], action["requires_confirmation"]) == ("read", False)
    items = goal["extract"]["items"]
    assert (items["type"], items["parse"], items["resolve"]) == ("regex", "json", "rows")
    assert set(action["output"]["fields"]) == {"name", "link", "followers"}
    assert "info" in action["output"]["fields"]["name"]  # the name came from the row the card points to
    # the executor ran the card against the site: rows filled in, fields projected
    assert mapped.validation.live[0].status == "SUCCESS", mapped.validation.live[0]


@pytest.mark.browser
@pytest.mark.xfail(reason="also fails on its own branch (PR 22): the POST is mapped as a read", strict=False)
def test_without_the_llm_the_tie_is_reported_and_a_persons_post_stays_a_write(stream_task):
    mapped = map_domain(stream_task, llm=LLM(enabled=False))
    [task] = mapped.tasks
    assert any("goal tie" in w for w in task.warnings)
    assert mapped.card["actions"][0]["kind"] == "write"  # recorded by a person: the method decides
