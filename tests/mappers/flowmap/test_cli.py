"""python -m spider.mappers.flowmap end to end on the bookshop: record (from a codegen script), map, show,
break, heal."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from spider.mappers.flowmap.cli import _is_local, main, parser


def test_parser_and_local_hosts() -> None:
    args = parser().parse_args(["map", "shop.test", "search", "--no-llm", "--pause", "0"])
    assert args.domain == "shop.test" and args.actions == ["search"] and args.no_llm and args.pause == 0
    assert _is_local("http://127.0.0.1:8910/") and _is_local("https://bookshop.test/")
    assert not _is_local("https://www.linkedin.com/")


def test_record_needs_name_value_pairs(tmp_path: Path) -> None:
    script = tmp_path / "s.py"
    script.write_text('page.goto("https://a.test/")\npage.get_by_label("Q").fill("x")\n')
    with pytest.raises(SystemExit, match="name=value"):
        main(["record", "--from-script", str(script), "--action", "a", "--input", "oops"])


@pytest.mark.browser
async def test_cli_end_to_end(bookshop: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("playwright.async_api")
    import asyncio

    script = tmp_path / "recording.py"
    script.write_text(
        "def run(playwright):\n"
        "    page = context.new_page()\n"
        f'    page.goto("{bookshop}/")\n'
        '    page.get_by_placeholder("Search books").click()\n'
        '    page.get_by_placeholder("Search books").fill("harbour")\n'
        '    page.get_by_label("Sort by").select_option("price")\n'
        '    page.get_by_placeholder("Search books").press("Enter")\n'
        '    expect(page.locator("#count")).to_be_visible()\n'
    )
    common = [
        "--db",
        str(tmp_path / "runtime.db"),
        "--flows",
        str(tmp_path / "flows"),
        "--captures",
        str(tmp_path / "captures"),
    ]
    rc = main(
        [
            "record",
            "--from-script",
            str(script),
            "--action",
            "search_books",
            "--domain",
            "bookshop.test",
            "--input",
            "query=harbour",
            "--example",
            "query=the",
            "--example",
            "sort_by=relevance",
            "--results",
            ".result h3",
            "--next",
            "#next",
            "--flows",
            str(tmp_path / "flows"),
        ]
    )
    assert rc == 0
    flow = json.loads((tmp_path / "flows" / "bookshop.test" / "search_books.json").read_text())
    assert flow["inputs"] == {
        "query": {"marker": True, "examples": ["harbour", "the"]},
        "sort_by": {"marker": False, "examples": ["price", "relevance"]},
    }

    # main() runs its own event loop, so run it in a thread
    rc = await asyncio.to_thread(main, ["map", "bookshop.test", "--no-llm", "--pause", "0", *common])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert '"version": 1' in out and '"tier": "http"' in out

    rc = main(["show", "bookshop.test", "--db", str(tmp_path / "runtime.db")])
    out = capsys.readouterr().out
    assert rc == 0 and "search_books  read  tier http" in out and "GET {{base_url}}/api/search" in out

    httpx.post(f"{bookshop}/_admin/break/endpoint").raise_for_status()
    rc = await asyncio.to_thread(main, ["heal", "bookshop.test", "search_books", "--pause", "0", *common])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert '"status": "succeeded"' in out and "/api/v2/search" in out and "remap_done" in out
