"""Step 10: M0 recon, an informational platform guess plus embedded-data hints."""

import json
import re
import subprocess
import sys

import pytest
from synthetic import har_entry, make_bundle

from harmap.pipeline import map_domain, write_outputs
from harmap.pipeline.m0_recon import recon

SITE = "https://shop.example.com"


def _entries(*raws):
    b = make_bundle("m1", list(raws), domain="example.com")
    return [*b.entries, *(e for e, _ in b.dropped)]


def _page(body, path="/", **kw):
    return har_entry("GET", f"{SITE}{path}", mime="text/html", body=body, resource_type="document", **kw)


def test_platform_signatures():
    r = recon(
        _entries(
            _page('<link href="//cdn.shopify.com/s/files/theme.css">'),
            _page('<meta name="generator" content="WordPress 6.6">', path="/blog"),
            har_entry(
                "POST", f"{SITE}/api/graphql", post="{}", post_mime="application/json", body={"data": {}}
            ),
            har_entry(
                "GET", f"{SITE}/voyager/api/me", response_headers={"x-restli-protocol-version": "2.0.0"}
            ),
        )
    )
    assert set(r.platforms) == {"Shopify", "WordPress", "GraphQL API", "LinkedIn Rest.li API"}
    assert r.platforms["WordPress"] == ["body of GET /blog"]


def test_json_ld_types_and_embedded_blob():
    blob = json.dumps({"props": {"pageProps": {"items": [1, 2]}}, "buildId": "b1", "page": "/x"})
    html = (
        '<script type="application/ld+json">{"@type": "Organization", "name": "Acme"}</script>'
        f'<script id="__NEXT_DATA__" type="application/json">\n{blob}\n</script>'
    )
    r = recon(_entries(_page(html)))
    assert r.json_ld_types == ["Organization"]
    assert "Next.js" in r.platforms
    [emb] = r.embedded
    assert emb.script_id == "__NEXT_DATA__" and emb.top_keys == ["buildId", "page", "props"]
    assert emb.extract["parse"] == "json"
    # works without a DOTALL flag, as the executor runs it (re.findall), even across newlines
    assert [json.loads(m) for m in re.findall(emb.extract["expr"], html)][0]["buildId"] == "b1"


def test_nothing_found():
    r = recon(_entries(har_entry("GET", f"{SITE}/api/x", body={"a": 1})))
    assert not r.platforms and not r.embedded and not r.json_ld_types


@pytest.mark.browser
def test_fixture_site_recon_in_report_and_cli(fixture_captures, tmp_path):
    for run in ("m1", "m2", "real"):
        fixture_captures("search", run)
    domain_dir = fixture_captures.root / "127.0.0.1"
    result = map_domain(domain_dir, task="search")
    assert "Next.js" in result.recon.platforms
    [emb] = [e for e in result.recon.embedded if e.page == "/search"]
    assert emb.script_id == "__NEXT_DATA__"
    page = next(e for e in result.tasks[0].runs["m1"].entries if e.path == "/search")
    data = json.loads(re.findall(emb.extract["expr"], page.response_body)[0])
    assert data["props"]["pageProps"]["page"] == "search"

    _, report_path = write_outputs(result, tmp_path)
    report = report_path.read_text()
    assert "## Site recon (M0, informational)" in report and "**Next.js**" in report

    out = subprocess.run(
        [sys.executable, "-m", "harmap", "recon", str(domain_dir)], capture_output=True, text=True
    )
    assert out.returncode == 0 and "Next.js" in out.stdout and "__NEXT_DATA__" in out.stdout
