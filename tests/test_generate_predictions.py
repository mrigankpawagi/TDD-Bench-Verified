"""Tests for scripts/generate_predictions.py — pure functions and mocked integration tests."""

from __future__ import annotations

import io
import json
import tarfile
import threading
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

# The script lives in scripts/, not a package — import via importlib
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location(
    "generate_predictions",
    str(Path(__file__).resolve().parents[1] / "scripts" / "generate_predictions.py"),
)
gp = importlib.util.module_from_spec(_spec)
# Stub heavy third-party imports that are unavailable in CI
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("tqdm", MagicMock())
sys.modules.setdefault("tddbench", MagicMock())
sys.modules.setdefault("tddbench.harness", MagicMock())
sys.modules.setdefault("tddbench.harness.docker_build", MagicMock())
sys.modules.setdefault("tddbench.harness.docker_utils", MagicMock())
sys.modules.setdefault("tddbench.harness.test_spec", MagicMock())
_spec.loader.exec_module(gp)


# ── _make_tar ────────────────────────────────────────────────────────────────


class TestMakeTar:
    def test_creates_valid_tar_with_correct_content(self):
        data = b"hello world"
        tar_bytes = gp._make_tar("test.txt", data)
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r")
        members = tar.getnames()
        assert members == ["test.txt"]
        assert tar.extractfile("test.txt").read() == data

    def test_preserves_binary_content(self):
        data = bytes(range(256))
        tar_bytes = gp._make_tar("binary.bin", data)
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r")
        assert tar.extractfile("binary.bin").read() == data

    def test_empty_content(self):
        tar_bytes = gp._make_tar("empty.txt", b"")
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r")
        assert tar.extractfile("empty.txt").read() == b""

    def test_filename_with_path(self):
        tar_bytes = gp._make_tar("subdir/file.txt", b"data")
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r")
        assert "subdir/file.txt" in tar.getnames()


# ── _parse_status ────────────────────────────────────────────────────────────


class TestParseStatus:
    VALID = ["EXPLORE_DONE", "TEST_FAILS", "TEST_PASSES", "TEST_ERRORS"]

    def test_status_at_end_of_output(self):
        assert gp._parse_status("Some text\nSTATUS: TEST_FAILS", self.VALID) == "TEST_FAILS"

    def test_status_with_prefix_text(self):
        assert gp._parse_status("blah blah STATUS: EXPLORE_DONE", self.VALID) == "EXPLORE_DONE"

    def test_status_with_trailing_whitespace(self):
        assert gp._parse_status("STATUS: TEST_PASSES   \n", self.VALID) == "TEST_PASSES"

    def test_multiple_statuses_returns_last_valid(self):
        output = "STATUS: EXPLORE_DONE\nmore text\nSTATUS: TEST_FAILS\n"
        assert gp._parse_status(output, self.VALID) == "TEST_FAILS"

    def test_no_status_returns_none(self):
        assert gp._parse_status("just some output with no status", self.VALID) is None

    def test_empty_output_returns_none(self):
        assert gp._parse_status("", self.VALID) is None

    def test_invalid_status_not_in_valid_list(self):
        assert gp._parse_status("STATUS: GARBAGE", self.VALID) is None

    def test_later_invalid_after_earlier_valid(self):
        """If the last STATUS line has an invalid value, should scan back to find valid one."""
        output = "STATUS: EXPLORE_DONE\nSTATUS: GARBAGE\n"
        assert gp._parse_status(output, self.VALID) == "EXPLORE_DONE"

    def test_status_on_only_line(self):
        assert gp._parse_status("STATUS: TEST_ERRORS", self.VALID) == "TEST_ERRORS"

    def test_status_embedded_in_markdown(self):
        output = "```\nTest results:\n  2 passed\n```\nSTATUS: TEST_FAILS\n"
        assert gp._parse_status(output, self.VALID) == "TEST_FAILS"


# ── load_variant ─────────────────────────────────────────────────────────────


class TestLoadVariant:
    def test_simple_variant(self, tmp_path):
        v = tmp_path / "simple.yaml"
        v.write_text(yaml.dump({"model_name": "gpt-5", "prompt": "Hello {problem_statement}"}))
        result = gp.load_variant(str(v))
        assert result["model_name"] == "gpt-5"
        assert "{problem_statement}" in result["prompt"]

    def test_variant_with_base_inheritance(self, tmp_path):
        base = tmp_path / "base.yaml"
        base.write_text(yaml.dump({"model_name": "gpt-5", "prompt": "Base prompt"}))
        child = tmp_path / "child.yaml"
        child.write_text(yaml.dump({"base": "base.yaml", "autopilot": True}))
        result = gp.load_variant(str(child))
        assert result["model_name"] == "gpt-5"
        assert result["prompt"] == "Base prompt"
        assert result["autopilot"] is True
        assert "base" not in result

    def test_child_overrides_base_fields(self, tmp_path):
        base = tmp_path / "base.yaml"
        base.write_text(yaml.dump({"model_name": "gpt-5", "prompt": "old"}))
        child = tmp_path / "child.yaml"
        child.write_text(yaml.dump({"base": "base.yaml", "model_name": "gpt-6", "prompt": "new"}))
        result = gp.load_variant(str(child))
        assert result["model_name"] == "gpt-6"
        assert result["prompt"] == "new"

    def test_steps_variant(self, tmp_path):
        variant_data = {
            "model_name": "gpt-5",
            "steps": [
                {
                    "name": "explore",
                    "prompt": "Explore {problem_statement}",
                    "transitions": {"DONE": {"goto": "done"}},
                }
            ],
        }
        v = tmp_path / "multi.yaml"
        v.write_text(yaml.dump(variant_data))
        result = gp.load_variant(str(v))
        assert "steps" in result
        assert result["steps"][0]["name"] == "explore"


# ── validate_variant ─────────────────────────────────────────────────────────


class TestValidateVariant:
    def test_valid_single_prompt(self):
        gp.validate_variant({"prompt": "test", "model_name": "gpt-5"})

    def test_valid_steps(self):
        gp.validate_variant({
            "model_name": "gpt-5",
            "steps": [
                {
                    "name": "step1",
                    "prompt": "...",
                    "transitions": {"OK": {"goto": "done"}},
                }
            ],
        })

    def test_both_prompt_and_steps_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            gp.validate_variant({"prompt": "test", "steps": [{"name": "s"}]})

    def test_neither_prompt_nor_steps_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            gp.validate_variant({"model_name": "gpt-5"})

    def test_empty_steps_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            gp.validate_variant({"steps": []})

    def test_invalid_goto_target_raises(self):
        with pytest.raises(ValueError, match="invalid goto"):
            gp.validate_variant({
                "steps": [
                    {
                        "name": "s1",
                        "prompt": "...",
                        "transitions": {"OK": {"goto": "nonexistent"}},
                    }
                ]
            })

    def test_missing_transitions_raises(self):
        with pytest.raises(ValueError, match="missing 'transitions'"):
            gp.validate_variant({"steps": [{"name": "s1", "prompt": "..."}]})

    def test_goto_done_is_valid(self):
        """'done' is a special target, not a step name — should be allowed."""
        gp.validate_variant({
            "steps": [
                {
                    "name": "only",
                    "prompt": "...",
                    "transitions": {"DONE": {"goto": "done"}},
                }
            ],
        })

    def test_goto_another_step_is_valid(self):
        gp.validate_variant({
            "steps": [
                {
                    "name": "a",
                    "prompt": "...",
                    "transitions": {"OK": {"goto": "b"}},
                },
                {
                    "name": "b",
                    "prompt": "...",
                    "transitions": {"OK": {"goto": "done"}},
                },
            ],
        })

    def test_duplicate_step_names_raises(self):
        """Duplicate step names would silently overwrite in the dict — should be rejected."""
        variant = {
            "steps": [
                {
                    "name": "explore",
                    "prompt": "...",
                    "transitions": {"OK": {"goto": "done"}},
                },
                {
                    "name": "explore",
                    "prompt": "...",
                    "transitions": {"OK": {"goto": "done"}},
                },
            ],
        }
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            gp.validate_variant(variant)


# ── load_existing_results / save_result ──────────────────────────────────────


class TestResultIO:
    def test_load_nonexistent_returns_empty(self, tmp_path):
        assert gp.load_existing_results(tmp_path / "nope.json") == {}

    def test_save_and_reload_roundtrip(self, tmp_path):
        path = tmp_path / "out.json"
        lock = threading.Lock()
        result = {"instance_id": "test__test-1", "model_patch": "diff --git ...", "model_name_or_path": "gpt-5"}
        gp.save_result(path, result, lock)
        loaded = gp.load_existing_results(path)
        assert "test__test-1" in loaded
        assert loaded["test__test-1"]["model_patch"] == "diff --git ..."

    def test_save_multiple_results(self, tmp_path):
        path = tmp_path / "out.json"
        lock = threading.Lock()
        for i in range(3):
            gp.save_result(path, {"instance_id": f"id-{i}", "model_patch": f"p{i}"}, lock)
        loaded = gp.load_existing_results(path)
        assert len(loaded) == 3

    def test_save_overwrites_same_instance_id(self, tmp_path):
        path = tmp_path / "out.json"
        lock = threading.Lock()
        gp.save_result(path, {"instance_id": "x", "model_patch": "old"}, lock)
        gp.save_result(path, {"instance_id": "x", "model_patch": "new"}, lock)
        loaded = gp.load_existing_results(path)
        assert len(loaded) == 1
        assert loaded["x"]["model_patch"] == "new"

    def test_save_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "out.json"
        lock = threading.Lock()
        gp.save_result(path, {"instance_id": "x", "model_patch": ""}, lock)
        assert path.exists()


# ── _run_multi_step ──────────────────────────────────────────────────────────


class TestRunMultiStep:
    """Tests for the multi-step state machine with mocked container."""

    @staticmethod
    def _make_variant(steps, max_retries=3):
        return {"steps": steps, "max_retries": max_retries}

    @staticmethod
    def _make_step(name, transitions, prompt="Do stuff. {problem_statement}"):
        return {"name": name, "prompt": prompt, "transitions": transitions}

    @staticmethod
    def _mock_container(outputs: list[tuple[str, bool, float]]):
        """Create a mock container whose exec_run_with_timeout returns successive outputs."""
        container = MagicMock()
        container.put_archive = MagicMock()
        return container, outputs

    def _run(self, variant, outputs, timeout=1800):
        container = MagicMock()
        container.put_archive = MagicMock()
        logger = MagicMock()

        call_idx = [0]
        def mock_exec(container_, cmd, timeout=None):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx < len(outputs):
                return outputs[idx]
            return ("", False, 0.0)

        with patch.object(gp, "exec_run_with_timeout", side_effect=mock_exec):
            result = gp._run_multi_step(
                variant=variant,
                container=container,
                copilot_path="/usr/local/bin/copilot",
                model_name="gpt-5",
                extra_flags=" --deny-tool=url",
                env_prefix="cd /testbed && ",
                problem_statement="Fix the bug.",
                total_timeout=timeout,
                logger=logger,
            )
        return result, container, logger

    def test_happy_path_three_steps(self):
        """explore → write_test → verify → done"""
        variant = self._make_variant([
            self._make_step("explore", {"EXPLORE_DONE": {"goto": "write_test"}}),
            self._make_step("write_test", {"TEST_FAILS": {"goto": "verify"}}),
            self._make_step("verify", {"VERIFIED": {"goto": "done"}}),
        ])
        outputs = [
            ("Explored the codebase.\nSTATUS: EXPLORE_DONE", False, 10.0),
            ("Wrote tests.\nSTATUS: TEST_FAILS", False, 20.0),
            ("Verified.\nSTATUS: VERIFIED", False, 15.0),
        ]
        result, container, logger = self._run(variant, outputs)
        assert "=== Step: explore ===" in result
        assert "=== Step: write_test ===" in result
        assert "=== Step: verify ===" in result

    def test_retry_on_loop_back(self):
        """write_test returns TEST_PASSES → loops back → then succeeds."""
        variant = self._make_variant([
            self._make_step("explore", {"EXPLORE_DONE": {"goto": "write_test"}}),
            self._make_step("write_test", {
                "TEST_FAILS": {"goto": "verify"},
                "TEST_PASSES": {"goto": "write_test", "instruction": "Rewrite the test."},
            }),
            self._make_step("verify", {"VERIFIED": {"goto": "done"}}),
        ])
        outputs = [
            ("STATUS: EXPLORE_DONE", False, 5.0),
            ("STATUS: TEST_PASSES", False, 10.0),  # loop back
            ("STATUS: TEST_FAILS", False, 10.0),   # succeeds
            ("STATUS: VERIFIED", False, 10.0),
        ]
        result, _, logger = self._run(variant, outputs)
        assert "=== Step: verify ===" in result

    def test_max_retries_exceeded(self):
        """Stops after max_retries even if steps keep looping."""
        variant = self._make_variant(
            [self._make_step("s", {"LOOP": {"goto": "s", "instruction": "Try again."}})],
            max_retries=2,
        )
        outputs = [
            ("STATUS: LOOP", False, 1.0),
            ("STATUS: LOOP", False, 1.0),
            ("STATUS: LOOP", False, 1.0),  # 3rd retry > max_retries=2
            ("STATUS: LOOP", False, 1.0),  # should not reach
        ]
        result, _, logger = self._run(variant, outputs)
        # Should have logged a warning about max retries
        logger.warning.assert_any_call("Max retries (2) exceeded. Stopping.")

    def test_timeout_budget_exhausted(self):
        """Stops when total timeout budget is used up."""
        variant = self._make_variant([
            self._make_step("s1", {"OK": {"goto": "s2"}}),
            self._make_step("s2", {"OK": {"goto": "done"}}),
        ])
        outputs = [
            ("STATUS: OK", False, 1800.0),  # uses entire budget
        ]
        result, _, logger = self._run(variant, outputs, timeout=1800)
        logger.warning.assert_any_call("Total timeout budget exhausted across steps.")

    def test_missing_status_retries_same_step(self):
        """No STATUS line → re-prompts same step, then succeeds."""
        variant = self._make_variant([
            self._make_step("s", {"DONE": {"goto": "done"}}),
        ])
        outputs = [
            ("Some output without status", False, 5.0),  # no STATUS
            ("STATUS: DONE", False, 5.0),
        ]
        result, _, logger = self._run(variant, outputs)
        assert "=== Step: s ===" in result

    def test_timed_out_step_stops(self):
        """If a step times out, the state machine stops immediately."""
        variant = self._make_variant([
            self._make_step("s", {"OK": {"goto": "done"}}),
        ])
        outputs = [
            ("partial output...", True, 1800.0),  # timed_out=True
        ]
        result, _, logger = self._run(variant, outputs)
        logger.warning.assert_any_call("Timed out during step 's'")

    def test_first_step_uses_yolo_subsequent_use_continue(self):
        """First invocation uses --model/--yolo, subsequent use --continue."""
        variant = self._make_variant([
            self._make_step("s1", {"OK": {"goto": "s2"}}),
            self._make_step("s2", {"OK": {"goto": "done"}}),
        ])
        outputs = [
            ("STATUS: OK", False, 5.0),
            ("STATUS: OK", False, 5.0),
        ]
        commands_seen = []

        def capture_exec(container_, cmd, timeout=None):
            commands_seen.append(cmd)
            idx = len(commands_seen) - 1
            return outputs[idx] if idx < len(outputs) else ("", False, 0.0)

        container = MagicMock()
        logger = MagicMock()
        with patch.object(gp, "exec_run_with_timeout", side_effect=capture_exec):
            gp._run_multi_step(
                variant=variant, container=container,
                copilot_path="/usr/local/bin/copilot", model_name="gpt-5",
                extra_flags=" --deny-tool=url", env_prefix="",
                problem_statement="Fix bug.", total_timeout=1800, logger=logger,
            )

        # First command should have --model and --yolo
        first_cmd_str = str(commands_seen[0])
        assert "--model" in first_cmd_str
        assert "--yolo" in first_cmd_str
        assert "--continue" not in first_cmd_str

        # Second should have --continue and --yolo
        second_cmd_str = str(commands_seen[1])
        assert "--continue" in second_cmd_str
        assert "--yolo" in second_cmd_str

    def test_problem_statement_formatted_in_prompt(self):
        """Verify {problem_statement} is substituted in the prompt."""
        variant = self._make_variant([
            self._make_step("s", {"OK": {"goto": "done"}},
                            prompt="Issue: {problem_statement}"),
        ])
        outputs = [("STATUS: OK", False, 5.0)]
        archives = []

        container = MagicMock()
        container.put_archive = lambda path, data: archives.append(data)
        logger = MagicMock()

        with patch.object(gp, "exec_run_with_timeout",
                          side_effect=lambda *a, **kw: outputs.pop(0)):
            gp._run_multi_step(
                variant=variant, container=container,
                copilot_path="copilot", model_name="m",
                extra_flags="", env_prefix="",
                problem_statement="The foo is broken.",
                total_timeout=1800, logger=logger,
            )

        # Extract prompt from the tar archive written to the container
        tar = tarfile.open(fileobj=io.BytesIO(archives[0]), mode="r")
        prompt_content = tar.extractfile("prompt.txt").read().decode()
        assert "The foo is broken." in prompt_content
        assert "{problem_statement}" not in prompt_content


# ── load_benchmark ───────────────────────────────────────────────────────────


class TestLoadBenchmark:
    def test_loads_json_array(self, tmp_path):
        data = [{"instance_id": "a"}, {"instance_id": "b"}]
        path = tmp_path / "bench.json"
        path.write_text(json.dumps(data))
        loaded = gp.load_benchmark(str(path))
        assert len(loaded) == 2
        assert loaded[0]["instance_id"] == "a"


# ── Variant file integration tests ──────────────────────────────────────────


class TestVariantFiles:
    """Test that actual variant YAML files in the repo are valid."""

    VARIANTS_DIR = Path(__file__).resolve().parents[1] / "variants"

    @pytest.mark.parametrize("yaml_file", list((Path(__file__).resolve().parents[1] / "variants").glob("*.yaml")))
    def test_variant_loads_and_validates(self, yaml_file):
        variant = gp.load_variant(str(yaml_file))
        assert "model_name" in variant
        gp.validate_variant(variant)
