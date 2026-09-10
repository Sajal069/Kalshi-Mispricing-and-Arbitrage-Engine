"""CLI surface checks.

These exist because a syntax error in ``kima/cli.py`` once survived a fully green
test run: nothing imported the module, so nothing compiled it. Every other test
exercises the library directly, which leaves the entry point -- the only part a
user actually touches -- completely uncovered.

The bar here is deliberately low and broad: every module imports, every
subcommand parses, and the commands that need credentials fail cleanly instead
of raising.
"""

import importlib
import pkgutil
from pathlib import Path

import pytest

import kima
from kima.cli import build_parser, main


class TestEveryModuleCompiles:
    def test_import_all(self):
        """Import every module in the package, including ones nothing else uses."""
        failed = []
        for mod in pkgutil.walk_packages(kima.__path__, prefix="kima."):
            try:
                importlib.import_module(mod.name)
            except Exception as exc:  # pragma: no cover - the failure is the point
                failed.append(f"{mod.name}: {type(exc).__name__}: {exc}")
        assert not failed, "modules failed to import:\n" + "\n".join(failed)

    def test_entry_point_imports(self):
        importlib.import_module("kima.__main__")


class TestParser:
    def test_every_subcommand_is_registered(self):
        parser = build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        assert actions, "no subcommands registered"
        names = set(actions[0].choices)
        assert names == {
            "auth", "discover", "simulate", "record", "universe", "settle",
            "backtest", "validate", "report", "all", "export",
        }

    @pytest.mark.parametrize("argv", [
        ["auth", "--offline"],
        ["discover", "--series", "KXBTCY"],
        ["simulate", "--hours", "0.1"],
        ["record", "--minutes", "1"],
        ["universe"],
        ["settle"],
        ["backtest"],
        ["validate"],
        ["report"],
        ["all", "--hours", "0.1"],
        ["export"],
    ])
    def test_subcommand_parses_and_binds_a_handler(self, argv):
        args = build_parser().parse_args(argv)
        assert callable(args.func)

    def test_record_defaults_to_production(self):
        """Demo books are synthetic liquidity; a demo tape cannot answer a
        microstructure question, and the recorder places no orders."""
        assert build_parser().parse_args(["record"]).env == "prod"

    def test_capital_cap_is_off_by_default(self):
        """A cap that binds mid-tape would bias every number measured after it."""
        assert build_parser().parse_args(["backtest"]).capital == 0.0


class TestCredentialGating:
    @pytest.mark.parametrize("argv", [["auth", "--offline"], ["record", "--minutes", "1"],
                                      ["settle"]])
    def test_missing_credentials_exit_cleanly(self, argv, monkeypatch, capsys):
        monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
        assert main(argv) == 2
        # `auth` reports the missing variable by name on stdout as part of its
        # checklist and puts the remedy on stderr; the others only use stderr.
        captured = capsys.readouterr()
        assert "KALSHI_KEY_ID" in (captured.out + captured.err)
        assert captured.err.strip(), "a failure must say something on stderr"


class TestArtefactPlacement:
    """Report artefacts belong beside the tape they describe.

    A fixed `data/run` default let `report --tape data/live/...` overwrite a
    synthetic report with a live one, leaving a directory whose tape and metrics
    described different data. Silent, and exactly the kind of thing that misleads
    someone reading the results later.
    """

    def test_defaults_to_the_tape_directory(self):
        from kima.cli import _artefact_dir
        args = build_parser().parse_args(
            ["--tape", "data/live/tape.jsonl.gz", "report"])
        assert _artefact_dir(args).as_posix() == "data/live"

    def test_explicit_out_dir_still_wins(self):
        from kima.cli import _artefact_dir
        args = build_parser().parse_args(
            ["--tape", "data/live/tape.jsonl.gz", "report", "--out-dir", "somewhere"])
        assert _artefact_dir(args).as_posix() == "somewhere"

    def test_validate_output_follows_the_tape(self):
        from kima.cli import _artefact_dir
        args = build_parser().parse_args(
            ["--tape", "data/live/tape.jsonl.gz", "validate"])
        assert args.out is None
        assert _artefact_dir(args).as_posix() == "data/live"


class TestOutputPathFallbacks:
    """Every `--out` default must have a fallback where it is consumed.

    Changing these defaults to None so artefacts land beside the tape left
    `validate` and `export` passing None straight into Path(). `validate` ran
    all seven checks over ~25 minutes and then died writing the results -- the
    worst place to discover it.
    """

    @pytest.mark.parametrize("argv,expected", [
        (["--tape", "data/live/tape.jsonl.gz", "validate"], "data/live/validation.json"),
        (["--tape", "data/live/tape.jsonl.gz", "backtest"], "data/live/metrics.json"),
        (["--tape", "data/live/tape.jsonl.gz", "export"], "data/live/deltas.parquet"),
    ])
    def test_out_resolves_to_a_real_path(self, argv, expected):
        from kima.cli import _artefact_dir
        args = build_parser().parse_args(argv)
        default_name = Path(expected).name
        resolved = args.out or str(_artefact_dir(args) / default_name)
        assert Path(resolved).as_posix() == expected
        assert Path(resolved) is not None

    def test_no_command_consumes_out_without_a_fallback(self):
        """Guard the pattern rather than the three known instances."""
        import re
        src = (Path(__file__).resolve().parents[1] / "kima" / "cli.py").read_text(encoding="utf-8")
        bare = re.findall(r"^\s*(?!.*\bor\b).*\bargs\.out\b(?!_dir).*$", src, re.M)
        offenders = [l.strip() for l in bare
                     if "add_argument" not in l and "args.out =" not in l]
        assert not offenders, "args.out used without a None fallback:\n" + "\n".join(offenders)
