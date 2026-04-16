#!/usr/bin/env python3
"""Generate TDD test predictions by running Copilot CLI inside harness Docker containers."""

from __future__ import annotations

import json
import os
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
)
from tddbench.harness.test_spec import make_test_spec

COPILOT_INSTALL_CMD = "curl -fsSL https://gh.io/copilot-install | bash"
COPILOT_BIN = "/usr/local/bin/copilot"


def load_variant(variant_path: str) -> dict:
    with open(variant_path) as f:
        return yaml.safe_load(f)


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
        logger.info(f"Copilot CLI install output:\n{install_output[-500:]}")

        # Find the copilot binary
        which_result = container.exec_run(
            '/bin/bash -c "which copilot || find / -name copilot -type f 2>/dev/null | head -5"',
            user="root",
        )
        copilot_path = which_result.output.decode("utf-8").strip().split("\n")[0]
        if not copilot_path:
            raise RuntimeError("Copilot CLI binary not found after installation")
        logger.info(f"Copilot binary at: {copilot_path}")

        # Interpolate prompt
        prompt_template = variant["prompt"]
        prompt = prompt_template.format(
            problem_statement=instance.get("problem_statement", ""),
        )

        # Run Copilot CLI
        logger.info(f"Running Copilot CLI for {instance_id}...")
        copilot_output, timed_out, runtime = exec_run_with_timeout(
            container,
            [
                "/bin/bash", "-c",
                f"cd /testbed && GH_TOKEN={gh_token} GITHUB_TOKEN={gh_token} "
                f"{copilot_path} --model {model_name} --yolo --deny-tool=url -p {json.dumps(prompt)}",
            ],
            timeout=timeout,
        )
        logger.info(f"Copilot runtime: {runtime:.2f}s, timed_out: {timed_out}")
        logger.info(f"Copilot output:\n{copilot_output[-2000:]}")

        if timed_out:
            logger.warning(f"Copilot timed out for {instance_id}")

        # Capture git diff against base commit
        # Only include test files (test*.py) to filter out junk diffs
        base_commit = instance["base_commit"]
        container.exec_run("git add -N .", workdir="/testbed")
        git_diff = container.exec_run(
            f"git diff {base_commit} -- '**/test*.py'", workdir="/testbed"
        ).output.decode("utf-8").strip()
        logger.info(f"Git diff ({len(git_diff)} chars):\n{git_diff[:2000]}")

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


if __name__ == "__main__":
    main()
