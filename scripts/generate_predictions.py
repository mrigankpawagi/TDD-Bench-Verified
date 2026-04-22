#!/usr/bin/env python3
"""Generate TDD test predictions by running Copilot CLI inside harness Docker containers."""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import tempfile
import threading
import traceback
import yaml

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import docker
from tqdm import tqdm

from tddbench.harness.docker_build import (
    build_container,
    build_env_images,
    close_logger,
    setup_logger,
)
from tddbench.harness.docker_utils import (
    cleanup_container,
    exec_run_with_timeout,
    remove_image,
)
from tddbench.harness.test_spec import make_test_spec

COPILOT_INSTALL_CMD = "curl -fsSL https://gh.io/copilot-install | bash"
COPILOT_BIN = "/usr/local/bin/copilot"
COPILOT_JSON_FLAG = " --output-format json"


def _make_tar(name: str, data: bytes) -> bytes:
    """Create an in-memory tar archive containing a single file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.read()


def _parse_jsonl_to_text(jsonl_output: str) -> str:
    """Parse Copilot JSONL output into human-readable text with expanded tool calls."""
    lines = []
    for raw_line in jsonl_output.strip().split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            lines.append(raw_line)
            continue

        event_type = event.get("type", "")
        if event_type == "response":
            body = event.get("body", "")
            if body:
                lines.append(body)
        elif event_type == "tool_call":
            name = event.get("tool", event.get("name", "unknown"))
            args = event.get("arguments", event.get("args", {}))
            if isinstance(args, dict):
                args_str = json.dumps(args, indent=2)
            else:
                args_str = str(args)
            lines.append(f"\n>>> Tool call: {name}")
            lines.append(args_str)
        elif event_type == "tool_result":
            name = event.get("tool", event.get("name", ""))
            result = event.get("result", event.get("output", ""))
            if isinstance(result, dict):
                result_str = json.dumps(result, indent=2)
            else:
                result_str = str(result)
            header = f"<<< Tool result: {name}" if name else "<<< Tool result"
            lines.append(header)
            lines.append(result_str)
        elif event_type == "error":
            lines.append(f"[ERROR] {event.get('message', event.get('body', ''))}")
        elif event_type == "status":
            lines.append(f"[STATUS] {event.get('body', event.get('message', ''))}")
        else:
            # Unknown event type — include raw for debugging
            body = event.get("body", event.get("message", ""))
            if body:
                lines.append(str(body))

    return "\n".join(lines)


def _save_copilot_output(log_dir: Path, copilot_output: str, label: str, logger) -> None:
    """Save raw JSONL and parsed text log for a copilot invocation."""
    jsonl_path = log_dir / f"copilot_output_{label}.jsonl"
    jsonl_path.write_text(copilot_output)
    logger.info(f"Raw JSONL saved to {jsonl_path}")

    parsed = _parse_jsonl_to_text(copilot_output)
    logger.info(f"Copilot output ({label}):\n{parsed}")


def load_variant(variant_path: str) -> dict:
    with open(variant_path) as f:
        variant = yaml.safe_load(f)
    if "base" in variant:
        base_path = Path(variant_path).parent / variant.pop("base")
        base = load_variant(str(base_path))
        base.update(variant)
        return base
    return variant


def validate_variant(variant: dict) -> None:
    """Validate variant schema: must have exactly one of 'prompt' or 'steps'."""
    has_prompt = "prompt" in variant
    has_steps = "steps" in variant
    if has_prompt == has_steps:
        raise ValueError("Variant must have exactly one of 'prompt' or 'steps'")
    if has_steps:
        steps = variant["steps"]
        if not steps:
            raise ValueError("'steps' must be non-empty")
        step_names_list = [s["name"] for s in steps]
        if len(step_names_list) != len(set(step_names_list)):
            raise ValueError("Duplicate step names found in 'steps'")
        step_names = set(step_names_list)
        for step in steps:
            if "transitions" not in step:
                raise ValueError(f"Step '{step['name']}' is missing 'transitions'")
            for status, transition in step["transitions"].items():
                target = transition["goto"]
                if target != "done" and target not in step_names:
                    raise ValueError(
                        f"Step '{step['name']}' has invalid goto target '{target}'"
                    )


def _parse_status(output: str, valid_statuses: list[str]) -> str | None:
    """Parse STATUS: XXX from the last non-empty lines of copilot output."""
    for line in reversed(output.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^.*STATUS:\s*([A-Z0-9_]+)\s*$", line)
        if m and m.group(1) in valid_statuses:
            return m.group(1)
    return None


def load_benchmark(benchmark_path: str) -> list[dict]:
    with open(benchmark_path) as f:
        return json.load(f)


def load_existing_results(output_path: Path) -> dict[str, dict]:
    """Load existing results for resume support. Returns a dict keyed by instance_id."""
    if not output_path.exists():
        return {}
    with open(output_path) as f:
        results = json.load(f)
    return {r["instance_id"]: r for r in results}


def save_result(output_path: Path, result: dict, lock: threading.Lock):
    """Atomically append a single result to the output file (thread-safe)."""
    with lock:
        existing = load_existing_results(output_path)
        existing[result["instance_id"]] = result
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file + rename
        fd, tmp_path = tempfile.mkstemp(
            dir=output_path.parent, suffix=".tmp", prefix=".predictions_"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(list(existing.values()), f, indent=2)
            os.replace(tmp_path, output_path)
        except Exception:
            os.unlink(tmp_path)
            raise


def _run_multi_step(
    variant: dict,
    container,
    copilot_path: str,
    model_name: str,
    extra_flags: str,
    env_prefix: str,
    problem_statement: str,
    total_timeout: int,
    logger,
    log_dir: Path | None = None,
) -> str:
    """Run a multi-step state machine of Copilot CLI invocations using --continue."""
    steps = {s["name"]: s for s in variant["steps"]}
    max_retries = variant.get("max_retries", 3)
    current_step = variant["steps"][0]["name"]
    is_first = True
    total_runtime = 0.0
    retry_count = 0
    step_count = 0
    all_output: list[str] = []

    while current_step != "done":
        if retry_count > max_retries:
            logger.warning(f"Max retries ({max_retries}) exceeded. Stopping.")
            break

        remaining_timeout = total_timeout - int(total_runtime)
        if remaining_timeout <= 0:
            logger.warning("Total timeout budget exhausted across steps.")
            break

        step = steps[current_step]

        # Build the prompt for this step
        prompt = step["prompt"].format(problem_statement=problem_statement)
        if not is_first and transition.get("instruction"):
            prompt = transition["instruction"].strip() + "\n\n" + prompt

        # Write prompt to file in container
        container.put_archive("/tmp", _make_tar("prompt.txt", prompt.encode()))

        if is_first:
            cmd = (
                f'{copilot_path} --model {model_name} --yolo{extra_flags}'
                f'{COPILOT_JSON_FLAG} -p "$(cat /tmp/prompt.txt)"'
            )
            is_first = False
        else:
            cmd = (
                f'{copilot_path} --continue --yolo{extra_flags}'
                f'{COPILOT_JSON_FLAG} -p "$(cat /tmp/prompt.txt)"'
            )

        logger.info(f"Step '{current_step}' starting (budget: {remaining_timeout}s)...")
        copilot_output, timed_out, runtime = exec_run_with_timeout(
            container,
            ["/bin/bash", "-c", f"{env_prefix}{cmd}"],
            timeout=remaining_timeout,
        )
        total_runtime += runtime
        logger.info(f"Step '{current_step}' runtime: {runtime:.2f}s, timed_out: {timed_out}")
        step_label = f"step_{step_count}_{current_step}"
        step_count += 1
        if log_dir:
            _save_copilot_output(log_dir, copilot_output, step_label, logger)
        else:
            logger.info(f"Step '{current_step}' output:\n{copilot_output}")
        all_output.append(f"=== Step: {current_step} ===\n{copilot_output}")

        if timed_out:
            logger.warning(f"Timed out during step '{current_step}'")
            break

        # Parse status from output
        valid_statuses = list(step["transitions"].keys())
        status = _parse_status(copilot_output, valid_statuses)

        if status is None:
            logger.warning(
                f"No valid STATUS found in step '{current_step}' output. "
                f"Expected one of: {valid_statuses}. Retrying same step."
            )
            retry_count += 1
            # Re-prompt asking for the status
            transition = {
                "goto": current_step,
                "instruction": (
                    "You did not end your response with a STATUS line. "
                    "Please complete the step and end with the appropriate "
                    f"STATUS: {' / '.join(valid_statuses)}"
                ),
            }
            continue

        logger.info(f"Step '{current_step}' returned STATUS: {status}")
        transition = step["transitions"][status]
        next_step = transition["goto"]

        # Track retries when looping back
        if next_step != "done" and next_step != current_step:
            # Moving forward — check if we're going back to a prior step
            step_order = [s["name"] for s in variant["steps"]]
            if (next_step in step_order and current_step in step_order
                    and step_order.index(next_step) < step_order.index(current_step)):
                retry_count += 1
        elif next_step == current_step:
            retry_count += 1

        current_step = next_step

    logger.info(f"Multi-step total runtime: {total_runtime:.2f}s, retries used: {retry_count}")
    return "\n".join(all_output)


def run_instance_prediction(
    instance: dict,
    variant: dict,
    gh_token: str,
    client: docker.DockerClient,
    run_id: str,
    timeout: int,
    output_path: Path,
    file_lock: threading.Lock,
) -> str:
    """Run Copilot CLI on a single instance and capture the generated test patch."""
    # Build a TestSpec — the instance needs a test_patch field (can be empty)
    instance_for_spec = dict(instance)
    if "test_patch" not in instance_for_spec or not instance_for_spec["test_patch"]:
        instance_for_spec["test_patch"] = ""

    test_spec = make_test_spec(instance_for_spec)
    instance_id = test_spec.instance_id
    model_name = variant["model_name"]

    log_dir = Path("logs") / "generate_predictions" / run_id / instance_id
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(instance_id, log_dir / "generate.log")

    container = None
    try:
        # Build + start container
        container = build_container(test_spec, client, run_id, logger, False, False)
        container.start()
        logger.info(f"Container for {instance_id} started: {container.id}")

        # Install Copilot CLI
        logger.info("Installing Copilot CLI...")
        install_output, timed_out, _ = exec_run_with_timeout(
            container,
            f'/bin/bash -c "GH_TOKEN={gh_token} {COPILOT_INSTALL_CMD}"',
            timeout=300,
        )
        if timed_out:
            raise RuntimeError("Copilot CLI installation timed out")
        logger.info(f"Copilot CLI install output:\n{install_output}")

        # Find the copilot binary
        which_result = container.exec_run(
            '/bin/bash -c "which copilot || find / -name copilot -type f 2>/dev/null | head -5"',
            user="root",
        )
        copilot_path = which_result.output.decode("utf-8").strip().split("\n")[0]
        if not copilot_path:
            raise RuntimeError("Copilot CLI binary not found after installation")
        logger.info(f"Copilot binary at: {copilot_path}")

        container.exec_run("mkdir -p /tmp", workdir="/testbed")

        # Activate the conda environment so Copilot's shell commands can use
        # pytest and other project dependencies that are installed there.
        container.exec_run(
            ["/bin/bash", "-c",
             'echo "source /opt/miniconda3/bin/activate && conda activate testbed" >> ~/.bashrc'],
            workdir="/testbed",
        )

        # Build extra CLI flags
        extra_flags = " --deny-tool=url"
        if variant.get("autopilot"):
            extra_flags += " --autopilot"
        if variant.get("plan"):
            extra_flags += " --plan"

        problem_statement = instance.get("problem_statement", "")
        env_prefix = (
            f"source /opt/miniconda3/bin/activate && conda activate testbed && "
            f"cd /testbed && GH_TOKEN={gh_token} GITHUB_TOKEN={gh_token} "
        )

        if "steps" in variant:
            # ── Multi-step state machine ──
            copilot_output = _run_multi_step(
                variant, container, copilot_path, model_name,
                extra_flags, env_prefix, problem_statement,
                timeout, logger, log_dir,
            )
        else:
            # ── Single-prompt (backward compatible) ──
            prompt = variant["prompt"].format(problem_statement=problem_statement)
            container.put_archive("/tmp", _make_tar("prompt.txt", prompt.encode()))

            logger.info(f"Running Copilot CLI for {instance_id}...")
            copilot_output, timed_out, runtime = exec_run_with_timeout(
                container,
                ["/bin/bash", "-c",
                 f'{env_prefix}'
                 f'{copilot_path} --model {model_name} --yolo{extra_flags}'
                 f'{COPILOT_JSON_FLAG} '
                 f'-p "$(cat /tmp/prompt.txt)"'],
                timeout=timeout,
            )
            logger.info(f"Copilot runtime: {runtime:.2f}s, timed_out: {timed_out}")
            _save_copilot_output(log_dir, copilot_output, "single", logger)

            if timed_out:
                logger.warning(f"Copilot timed out for {instance_id}")

        # Capture git diff against base commit
        # Include test files and anything under test* directories
        base_commit = instance["base_commit"]
        container.exec_run("git add -N .", workdir="/testbed")
        git_diff = container.exec_run(
            f"git diff {base_commit} -- ':(glob)**/test*.py' ':(glob)**/test*/**'",
            workdir="/testbed",
        ).output.decode("utf-8").strip()
        logger.info(f"Git diff ({len(git_diff)} chars):\n{git_diff}")

        result = {
            "instance_id": instance_id,
            "model_patch": git_diff if git_diff else "",
            "model_name_or_path": model_name,
        }
        save_result(output_path, result, file_lock)
        logger.info(f"Result saved for {instance_id}")
        return instance_id

    except Exception as e:
        logger.error(f"Error for {instance_id}: {e}\n{traceback.format_exc()}")
        result = {
            "instance_id": instance_id,
            "model_patch": "",
            "model_name_or_path": variant["model_name"],
            "error": str(e),
        }
        save_result(output_path, result, file_lock)
        raise
    finally:
        cleanup_container(client, container, logger)
        remove_image(client, test_spec.instance_image_key, logger)
        close_logger(logger)


def main():
    parser = ArgumentParser(description="Generate TDD test predictions using Copilot CLI")
    parser.add_argument(
        "--benchmark", type=str, required=True,
        help="Path to benchmark JSON file (e.g., TDD_Bench_selected.json)",
    )
    parser.add_argument(
        "--variant", type=str, required=True,
        help="Path to variant YAML file (e.g., variants/basic.yaml)",
    )
    parser.add_argument(
        "--max_workers", type=int, default=1,
        help="Maximum number of parallel workers (default: 1)",
    )
    parser.add_argument(
        "--timeout", type=int, default=1800,
        help="Per-instance timeout in seconds (default: 1800)",
    )
    args = parser.parse_args()

    # Load inputs
    variant = load_variant(args.variant)
    validate_variant(variant)
    dataset = load_benchmark(args.benchmark)
    variant_name = Path(args.variant).stem
    run_id = f"generate-{variant_name}"

    # Determine output path
    output_path = Path("copilot") / f"{variant_name}.json"

    # Obtain GH_TOKEN
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not gh_token:
        print("GH_TOKEN/GITHUB_TOKEN not set. Trying `gh auth token`...")
        import subprocess
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if result.returncode == 0:
            gh_token = result.stdout.strip()
        else:
            print("Error: Could not obtain GitHub token. Set GH_TOKEN or run `gh auth login`.")
            return

    # Resume support — skip already completed instances
    existing_results = load_existing_results(output_path)
    completed_ids = {
        iid for iid, r in existing_results.items()
        if r.get("model_patch") and "error" not in r
    }
    remaining = [inst for inst in dataset if inst["instance_id"] not in completed_ids]

    print(f"Total instances: {len(dataset)}")
    print(f"Already completed: {len(completed_ids)}")
    print(f"Remaining: {len(remaining)}")
    print(f"Output: {output_path}")
    print(f"Variant: {variant_name} (model: {variant['model_name']})")
    print(f"Token: {gh_token[:5]}...{gh_token[-4:]}")

    if not remaining:
        print("All instances already completed.")
        return

    # Build environment images upfront
    client = docker.from_env()
    for inst in remaining:
        if "test_patch" not in inst or not inst["test_patch"]:
            inst["test_patch"] = ""
    build_env_images(client, remaining, force_rebuild=False, max_workers=args.max_workers)

    # Run instances
    file_lock = threading.Lock()
    print(f"Running {len(remaining)} instances with max_workers={args.max_workers}...")

    with tqdm(total=len(remaining), smoothing=0, desc="Generating predictions") as pbar:
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures = {
                executor.submit(
                    run_instance_prediction,
                    instance,
                    variant,
                    gh_token,
                    client,
                    run_id,
                    args.timeout,
                    output_path,
                    file_lock,
                ): instance["instance_id"]
                for instance in remaining
            }
            for future in as_completed(futures):
                pbar.update(1)
                instance_id = futures[future]
                try:
                    future.result()
                    print(f"  ✓ {instance_id}")
                except Exception as e:
                    print(f"  ✗ {instance_id}: {e}")

    # Final summary
    final_results = load_existing_results(output_path)
    successful = sum(1 for r in final_results.values() if r.get("model_patch") and "error" not in r)
    print(f"\nDone. {successful}/{len(dataset)} instances completed successfully.")
    print(f"Results saved to {output_path}")
    client.close()


if __name__ == "__main__":
    main()
