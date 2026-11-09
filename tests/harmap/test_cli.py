import argparse
import subprocess
import sys

import pytest

import spider.harmap
from spider.harmap import cli


def test_version_constants():
    assert spider.harmap.__version__ == "0.1.0"
    assert spider.harmap.SCHEMA_VERSION == "0.4"


def test_module_entrypoint_help():
    out = subprocess.run(
        [sys.executable, "-m", "spider.harmap", "--help"], capture_output=True, text=True, check=True
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


def test_cli_reads_dotenv_without_overriding_the_environment(tmp_path):
    (tmp_path / ".env").write_text("MAPPER_DOTENV_PROBE=from-dotenv\nMAPPER_DOTENV_KEEP=from-dotenv\n")
    code = (
        "import os; from mapper.cli import load_env; load_env(); "
        "print(os.environ.get('MAPPER_DOTENV_PROBE'), os.environ.get('MAPPER_DOTENV_KEEP'))"
    )
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("MAPPER_DOTENV")}
    env["MAPPER_DOTENV_KEEP"] = "from-shell"
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["from-dotenv", "from-shell"], out.stderr
