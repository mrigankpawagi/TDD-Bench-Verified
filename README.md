# GitHub Copilot on TDD-Bench

Evaluating GitHub Copilot on the TDD-Bench-Verified benchmark of reproduction tests for GitHub issues 

## Additional Setup

After following the setup instructions below, prepare a selected subset of the dataset for evaluation:

```bash
python scripts/prepare_selected_dataset.py -o TDD_Bench_selected.json
```

This randomly selects 25 items (seed `42`) from `TDD_Bench.json` and writes them to the specified output file. Use `-o`/`--output` (required) to set the output path, `-N` to change the number of items, and `--seed` to change the random seed.

## Generating Predictions

Use `scripts/generate_predictions.py` to run Copilot CLI inside Docker containers and generate test predictions:

```bash
python scripts/generate_predictions.py \
    --benchmark TDD_Bench_selected.json \
    --variant variants/basic.yaml
```

This will:
1. Build Docker images for each instance (reuses the existing harness)
2. Start a container per instance with the repo at the correct commit
3. Install and run Copilot CLI with the variant's prompt and model
4. Capture `git diff` as the prediction
5. Write results incrementally to `copilot/<variant_name>.json`

**Options:**
- `--max_workers N` — parallel instances (default: 1)
- `--timeout N` — per-instance timeout in seconds (default: 1800)

**Prerequisites:** Docker, a GitHub token with Copilot access (`GH_TOKEN` env var or `gh auth login`).

**Resume:** Re-running the same command skips already-completed instances.

**Evaluation:** Run the generated predictions through the evaluation harness:
```bash
python -m tddbench.harness.run_evaluation \
    --predictions_path copilot/basic.json \
    --max_workers 4 \
    --run_id copilot-basic
```

## Variants

Variant YAML files in `variants/` define the model and prompt template:

```yaml
model_name: claude-sonnet-4-20250514
prompt: |
  Your prompt here...
  {problem_statement}
```

The `{problem_statement}` placeholder is replaced with the instance's issue description.

**Optional fields:**
- `plan: true` — runs Copilot CLI with `--plan` mode, which creates a structured plan before executing.
- `autopilot: true` — runs Copilot CLI with `--autopilot` mode, which executes autonomously until the task is complete.
- `base: <filename>` — inherits all fields from another variant file (resolved relative to `variants/`), then applies any overrides. For example:
  ```yaml
  base: pro.yaml
  autopilot: true
  ```

**Multi-step variants:** Instead of a single `prompt`, a variant can define `steps` — a state machine of prompts that run sequentially using `copilot --continue` to maintain conversation context. Each step outputs a `STATUS:` line that the script uses to decide the next step (or loop back). See `variants/multiturn.yaml` for an example.

```yaml
max_retries: 3
steps:
  - name: explore
    prompt: |
      ...
      STATUS: EXPLORE_DONE
    transitions:
      EXPLORE_DONE:
        goto: write_test
        instruction: ""
  - name: write_test
    prompt: |
      ...
      STATUS: TEST_FAILS / TEST_PASSES / TEST_ERRORS
    transitions:
      TEST_FAILS:
        goto: verify
        instruction: ""
      TEST_PASSES:
        goto: write_test
        instruction: "The test passes. Rewrite it..."
```

## Results

Results on the selected-100 problems (default selection with `N=100`, `seed=42`):

| Approach | Resolved | Coverage | TDD Score |
|----------|----------|----------|-----------|
| Otter (GPT-4o) | 31 | 68.00% | 28.53% |
| Otter++ (GPT-4o) | 35 | 69.96% | 32.49% |
| Copilot Basic | 65 | 85.07% | 59.76% |
| Copilot Plus | 57 | 83.21% | 52.06% |
| Copilot Pro | 66 | 83.85% | 61.46% |
| Copilot ProPlus | 65 | — | — |
| Copilot MultiTurn | — | — | — |

<details>
<summary>Results on selected-25 (N=25, seed=42)</summary>

| Approach | Resolved | Coverage | TDD Score |
|----------|----------|----------|-----------|
| Otter (GPT-4o) | 13 | 66.17% | 45.34% |
| Otter++ (GPT-4o) | 13 | 67.91% | 45.34% |
| Copilot Basic | 18 | 82.38% | 65.90% |
| Copilot Plus | 18 | 80.78% | 64.11% |
| Copilot Pro | 19 | 80.38% | 67.71% |

</details>

- **Resolved**: number of instances where the generated tests fail before the fix and pass after.
- **Coverage**: average fraction of the fix's changed lines exercised by the generated tests.
- **TDD Score**: average of `coverage × fail_before × pass_after` across all instances.

# Original README

TDD-Bench-Verified is a new benchmark for generating test cases for test-driven development (TDD). Test-driven development, or TDD, is the practice of "test first, write code later", where a software developer writes tests before writing corresponding code. This means the tests initially fail, and, if everything goes right, they pass after applying the code changes. Compared to the common practice of "write first, test later", TDD makes requirements clearer, enhances confidence in the code once written, and leads to tests that emphasize the interface over implementation details.

TDD-Bench-Verified is derived from SWE-bench Verified. Each instance $x = (d_{issue}, c_{old})$ comprises a natural-language issue description $d_{issue}$ together with the original version of a codebase $c_{old}$ right before the issue was addressed. A prediction $y$ for an instance consists of a set of tests that should fail on $c_{old}$ and pass on $c_{new}$. However, solutions to TDD-Bench-Verified should predict $y$ without looking at $c_{new}$. This is a challenging task for large language models (LLMs). TDD-Bench-Verified contains 449 instance $x_i$, along with a Docker-based evaluation harness that evaluates a submission of predictions $y_i$. It checks the fail-to-pass criterion for each $y_i$, as well as measuring its code coverage on the code change from $c_{old}$ to $c_{new}$.

**Paper Link: [https://arxiv.org/pdf/2412.02883](https://arxiv.org/pdf/2412.02883)** 

<img src="Figures/tdd-github.png">



## 🚀 Set Up
TDD-bench uses Docker for reproducible evaluations just like SWE-Bench.
Follow the instructions in the [Docker setup guide](https://docs.docker.com/engine/install/) to install Docker on your machine. For additional assistance, you can also refer to the [SWE-Bench](https://github.com/princeton-nlp/SWE-bench) repository.

Finally, to build TDD-bench from source, follow these steps:
```bash
git clone https://github.ibm.com/tfahmed/TDD-Bench-Verified.git
cd TDD-Bench-Verified
pip install -e .
```

Generate TDD_Bench.json by running the following command. This json file will contain the complete dataset (which includes repository name, issue description, base commit SHA and other relevant information for 449 instances).
```bash
python dataset_preparation.py
```


Test your installation by running:
```bash
python -m tddbench.harness.run_evaluation \
    --predictions_path gold \
    --max_workers 1 \
    --instance_ids astropy__astropy-14995 \
    --run_id validate-gold
```


Evaluate model predictions on TDD-bench using the evaluation harness with the following command. This command will take the model generated test patches (--predictions_path) as input and report the $TDD_{Score}$ and number of fail-to-pass instances.
```bash
python -m tddbench.harness.run_evaluation \
    --dataset_name TDD_Bench.json \  
    --predictions_path <path_to_predictions> \
    --max_workers <num_workers> \
    --run_id <run_id>
#use --predictions_path 'gold' to verify the gold patches
#use --run_id to name the evaluation run

```


Use golden_test_patch.json formatting as a reference for "--predictions_path". The format is also shown below. 

```bash
[
    {
        "instance_id": "astropy__astropy-12907",
        "model_patch": "diff --git a/astropy/modeling/tests/test_separable.py b/astropy/modeling/tests/test_separable.py\n--- a/astropy/modeling/tests/test_separable.py\n+++ b/astropy/modeling/tests/test_separable.py\n@@ -28,6 +28,13 @@\n p1 = models.Polynomial1D(1, name='p1')\n \n \n+cm_4d_expected = (np.array([False, False, True, True]),\n+                  np.array([[True,  True,  False, False],\n+                            [True,  True,  False, False],\n+                            [False, False, True,  False],\n+                            [False, False, False, True]]))\n+\n+\n compound_models = {\n     'cm1': (map3 & sh1 | rot & sh1 | sh1 & sh2 & sh1,\n             (np.array([False, False, True]),\n@@ -52,7 +59,17 @@\n     'cm7': (map2 | p2 & sh1,\n             (np.array([False, True]),\n              np.array([[True, False], [False, True]]))\n-            )\n+            ),\n+    'cm8': (rot & (sh1 & sh2), cm_4d_expected),\n+    'cm9': (rot & sh1 & sh2, cm_4d_expected),\n+    'cm10': ((rot & sh1) & sh2, cm_4d_expected),\n+    'cm11': (rot & sh1 & (scl1 & scl2),\n+             (np.array([False, False, True, True, True]),\n+              np.array([[True,  True,  False, False, False],\n+                        [True,  True,  False, False, False],\n+                        [False, False, True,  False, False],\n+                        [False, False, False, True,  False],\n+                        [False, False, False, False, True]]))),\n }\n \n \n"
    },
    {
        "instance_id": "astropy__astropy-13033",
        "model_patch": "diff --git a/astropy/timeseries/tests/test_sampled.py b/astropy/timeseries/tests/test_sampled.py\n--- a/astropy/timeseries/tests/test_sampled.py\n+++ b/astropy/timeseries/tests/test_sampled.py\n@@ -395,6 +395,14 @@ def test_required_columns():\n     assert exc.value.args[0] == (\"TimeSeries object is invalid - expected \"\n                                  \"'time' as the first column but found 'banana'\")\n \n+    # https://github.com/astropy/astropy/issues/13009\n+    ts_2cols_required = ts.copy()\n+    ts_2cols_required._required_columns = ['time', 'a']\n+    with pytest.raises(ValueError) as exc:\n+        ts_2cols_required.remove_column('a')\n+    assert exc.value.args[0] == (\"TimeSeries object is invalid - expected \"\n+                                 \"['time', 'a'] as the first columns but found ['time', 'b']\")\n+\n \n @pytest.mark.parametrize('cls', [BoxLeastSquares, LombScargle])\n def test_periodogram(cls):\n"
    },
]
```


All the experiments were done using python 3.12.4. The requirement is Python 3.11 or later. List of Pre-Requisites:
```bash
beautifulsoup4
datasets
docker
ghapi
python-dotenv
requests
unidiff
tqdm
pytest
cldk
```
## Reference
If you use this benchmark, please consider citing our works.
```
@article{ahmed2024tdd,
  title={TDD-Bench Verified: Can LLMs Generate Tests for Issues Before They Get Resolved?}, 
  author={Ahmed, Toufique and Hirzel, Martin and Pan, Rangeet and Shinnar, Avraham and Sinha, Saurabh},
  journal={arXiv preprint arXiv:2412.02883},
  year={2024} 
}
```
**Paper Link: [https://arxiv.org/pdf/2412.02883](https://arxiv.org/pdf/2412.02883)** 

```
@article{ahmed2025otter,
  title={Otter: Generating Tests from Issues to Validate SWE Patches},
  author={Ahmed, Toufique and Ganhotra, Jatin and Pan, Rangeet and Shinnar, Avraham and Sinha, Saurabh and Hirzel, Martin},
  booktitle={International Conference on Machine Learning},
  year={2025}
}
```
**Paper Link: [https://arxiv.org/pdf/2502.05368](https://arxiv.org/pdf/2502.05368)**  

**Poster Link: [https://github.com/IBM/TDD-Bench-Verified/blob/main/Figures/ICML_Poster.pdf](https://github.com/IBM/TDD-Bench-Verified/blob/main/Figures/ICML_Poster.pdf)**

<img src="Figures/otter.png">

Otter has been accepted to the main technical track of ICML 2025. The tests generated by Otter can be accessed via the following link.
 
[Otter(GPT-4o)](https://github.com/IBM/TDD-Bench-Verified/blob/main/Otter/Otter_TDD_GPT4o.json)

[Otter++(GPT-4o)](https://github.com/IBM/TDD-Bench-Verified/blob/main/Otter/Otter_Plus_TDD_GPT4o.json)

This research was conducted by Toufique Ahmed, Jatin Ganhotra, Rangeet Pan, Avraham Shinnar, Saurabh Sinha, and Martin Hirzel.  



