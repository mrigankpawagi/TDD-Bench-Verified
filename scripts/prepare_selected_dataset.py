#!/usr/bin/env python3
"""Randomly select items from TDD_Bench.json and write to the specified output file."""

import argparse
import json
import random
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Randomly select items from TDD_Bench.json"
    )
    parser.add_argument(
        "-N", type=int, default=25, help="Number of items to select (default: 25)"
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Random seed (default: 42)"
    )
    parser.add_argument(
        "-o", "--output", type=Path, required=True, help="Path to the output JSON file"
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    input_path = repo_root / "TDD_Bench.json"
    output_path = args.output

    with open(input_path) as f:
        data = json.load(f)

    if args.N > len(data):
        parser.error(f"-N ({args.N}) exceeds available items ({len(data)})")

    random.seed(args.seed)
    selected = random.sample(data, args.N)

    with open(output_path, "w") as f:
        json.dump(selected, f, indent=2)

    print(f"Selected {len(selected)} items (seed={args.seed}) -> {output_path}")


if __name__ == "__main__":
    main()
