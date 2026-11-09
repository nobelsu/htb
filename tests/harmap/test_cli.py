import argparse
import subprocess
import sys

import pytest

import harmap
from harmap import cli


def test_version_constants():
    assert harmap.__version__ == "0.1.0"
    assert harmap.SCHEMA_VERSION == "0.4"


def test_module_entrypoint_help():
    out = subprocess.run(
        [sys.executable, "-m", "harmap", "--help"], capture_output=True, text=True, check=True
    )
    for command in ("capture", "map", "validate"):
        assert command in out.stdout


def test_capture_args_parse():
    args = cli.build_parser().parse_args(
        [
            "capture",
            "http://127.0.0.1:8000/",
            "--task",
            "search",
            "--run",
            "m1",
            "--marker",
            "query=zqxv8841",
            "--kind",
            "read",
        ]
    )
    assert args.task == "search"
    assert args.run == "m1"
    assert args.marker == [("query", "zqxv8841")]
    assert args.input == []
    assert args.headless is False


def test_map_args_parse():
    args = cli.build_parser().parse_args(["map", "captures/example.com/", "--task", "search"])
    assert args.captures_dir == "captures/example.com/"
    assert args.task == "search"


@pytest.mark.parametrize("bad", ["query", "=x", "query=", "_page=1"])
def test_key_value_rejects_bad(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_key_value(bad)


def test_run_choice_enforced():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["capture", "http://x/", "--task", "t", "--run", "m3"])


def test_command_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])
