# Comparison report JSON format

`solver-benchmark report INPUT [INPUT ...] --output comparison.html` writes
`comparison.html` and `comparison.json` beside each other. The JSON is an aggregated
summary for agents and scripts; the original `results.jsonl` files retain raw
trials. Both reports use one collected summary, including the same column minima
and relative ratios. No worker or binary input files are required to read them.
The command can regenerate an existing report pair, but refuses to replace an
existing JSON file unless it identifies itself as a version 1.0 or 1.1 comparison report. New reports use version 1.1; version 1.0 used a matrix-wide baseline for `relative_times`, while version 1.1 uses the comparison-platform baseline.
For example, `--output results/run/summary.html` is rejected if its automatic
`summary.json` destination already holds the benchmark run's outcome summary.

## Document identity and metadata

| Field | Meaning |
| --- | --- |
| `schema_version` | `"1.1"`, the JSON summary format version. |
| `report_type` | `"solver-benchmark-comparison"`, distinguishing this from raw results and run summaries. |
| `title` | The report title, including a custom `--title`. |
| `metric` | `"wall_seconds"` by default, or `"cpu_seconds"` with `--metric cpu`. All absolute timings use seconds. |
| `aggregation` | Explicit baseline/measured-trial selection, timing reductions, critical-path phases, normalization, and backward-error rules. |
| `sources` | Objects with a unique 1-based `id`, the input `results.jsonl` path, its execution `run_id`, and user-supplied `label` (or `null`). Paths are provenance references; the summary can be read without those files. |
| `excluded_trace_results` | Number of trace execution records excluded from timing and error aggregation (their RSS snapshots may contribute to matching rows). |
| `matrices` | One comparison per delivered matrix/RHS input hash. Trace-only inputs produce an empty array. |

Each matrix object contains `case_ids` (sorted case names), `input_hash`, `case`
(a source matrix/RHS recipe), `best_times`, `platform_best_times`, `platform_best_median_times`, and `rows`. Aliases with the same
delivered input hash share a comparison; `case` is the recipe from the first
matching source. Distinct tolerance settings remain distinct solver rows.

## Solver rows

| Field | Meaning |
| --- | --- |
| `platform`, `platform_id`, `host` | Human-readable platform label, hashed platform identity, and full recorded host/affinity metadata. |
| `comparison_platform`, `comparison_platform_id` | Comparison label and hash of recorded host metadata excluding VM hostname and cloud zone. Affinity grouping follows the policy described below. Distinct hosts remain separate rows. |
| `solver`, `configuration_id`, `configuration` | Solver ID, hashed configuration identity, and options, worker identity/build/dependency hashes, arguments, and recorded environments. |
| `label` | User-supplied input label, or `null` for unlabeled inputs. Matching results with different labels are separate rows; matching results with the same label may pool. |
| `sources` | Source IDs contributing to this row; join with the top-level `sources[].id`. |
| `outcomes` | Execution outcome names mapped to counts, including failures and unsupported configurations. |
| `trace_outcomes` | Trace execution outcome names mapped to counts, separate from baseline outcomes. An empty object indicates no matching trace result. |
| `samples` | Number of accuracy-accepted measured trials. Individual phase timings can still be missing. |
| `times` | Fastest accepted measured timings in seconds, keyed by phase. |
| `median_times` | Median accepted measured timing in seconds for each phase, pooled over all matching sources. |
| `cpu_times` | Independent minimum accepted baseline phase CPU seconds and minimum same-trial CPU critical path. |
| `phase_rss_after_bytes` | Minimum RSS snapshot after each completed phase in accepted measured trace trials; boundary snapshots are not phase peaks. |
| `cpu_utilization` | CPU seconds divided by wall seconds from the same accepted trial with the shortest positive wall time for each phase; critical path pairs same-trial sums. |
| `system_cpu_seconds`, `system_cpu_percent` | Kernel-mode CPU seconds and its percentage of total CPU seconds for the same selected trial as `cpu_utilization`; critical path requires all three system CPU times. Missing system timings produce null, including for older results; a zero total CPU time has no percentage. |
| `process_resources` | Minimum lifetime CPU seconds and peak RSS bytes, and CPU / wall, system CPU seconds and its percentage of total from the same successful baseline process with accepted trials and shortest positive lifetime wall time. Includes startup and warmup. |
| `relative_times` | Dimensionless timing ratios against the row's entry in `platform_best_times`, with the same phase keys. |
| `relative_median_times` | Median timing ratios against the row's entry in `platform_best_median_times`. |
| `worst_componentwise_backward_error` | Maximum dimensionless error over all RHS columns and measured baseline trials across the row's sources, including rejected solutions. |

The timing keys are `setup`, `pre_analyze`, `analysis`, `numeric`, `solve`,
`teardown`, and `critical_path`. Phase times are independent minima over accepted
measured baseline trials. `critical_path` is the minimum same-trial sum of
`analysis + numeric + solve`, excluding setup, preparation, teardown, IPC, and
parent validation. The median critical path is the median of those same-trial sums,
not the sum of phase medians. Median values pool individual eligible trials across
sources; for an even count, the middle two values are averaged. Warmups and trace
passes contribute neither timings nor errors.

Use `--label INPUT LABEL` for each labeled input when generating a report; `INPUT`
must also be listed among the positional inputs. Normally a directory and its
`results.jsonl` resolve to the same input. Labels do not establish Git provenance
or select a reference for relative timings. Unlabeled inputs retain the existing
grouping behavior and do not pool with labeled inputs. In the Python API,
`collect_report` and `write_report` accept an `input_labels` mapping from input
paths to labels.

When `results.jsonl` is a symlink, a directory input reads companion metadata
from that directory, while a file input follows the symlink and reads metadata
from the target's directory. A directory label selects its corresponding
directory input; a file label that matches two supplied sources is rejected as
ambiguous.

For each matrix, `best_times` holds each column's minimum across all solver/platform
rows, excluding missing values, retained for compatibility. `platform_best_times` maps each comparison platform ID to its own per-column minima. `relative_times` divides by the matching platform baseline: 1 is
best and 1.2 means 20% slower. Zero ties normalize to 1; positive times against a
zero baseline have an infinite ratio. Backward errors are always absolute and
are never normalized by another solver's error.
`platform_best_median_times` and `relative_median_times` apply the same rules to
the median for each row, independently of the minimum-based baselines.

By default, the comparison platform includes the recorded host and worker CPU
affinities. A single-core cloud run with consistent recorded affinity-control
metadata is grouped using its original available CPU mask, allowing comparison
with a run on that same mask. The pinned CPU set is retained in each row.
Pass `--compare-affinity` when intentionally comparing different
affinity limits, such as a cloud `--single-core` run against an available-core
run. This option ignores only those affinity fields when choosing relative
baselines; hardware and other platform fields still distinguish groups, and the
original platform identity and recorded affinities remain on each row. The HTML
and `aggregation.relative_times.affinity` disclose the chosen grouping policy.
Different CPU sets can affect performance; interpret this comparison accordingly.

## Numeric representation

Finite values are JSON numbers with their stored precision; HTML displays six
significant digits. `null` means no eligible timing or no recorded error, not zero.
Native-worker phase timing includes sampling overhead; allow for uncertainty on
the order of several microseconds, especially for short phases. This is a rule
of thumb, not a measured confidence interval or a guaranteed bound; scheduling
and other effects can cause larger variations.
System CPU times in short phases can be limited by operating system clock precision;
independent sampling boundaries and rounding can yield percentages above 100%.
Nonfinite diagnostics are JSON objects such as `{"nonfinite": "+inf"}`. This keeps
the file valid for strict JSON parsers and preserves the distinction between
missing measurements and infinite ratios/errors. A failed-verification diagnostic
may produce an infinite backward error even when no timing is accepted.

For example, a row may contain:

```json
{
  "solver": "example-solver",
  "sources": [1, 2],
  "samples": 6,
  "outcomes": {"success": 2},
  "times": {
    "setup": 0.0000012,
    "pre_analyze": 0.00002,
    "analysis": 0.00006,
    "numeric": 0.00013,
    "solve": 0.00002,
    "teardown": null,
    "critical_path": 0.00022
  },
  "relative_times": {
    "setup": 1.2,
    "pre_analyze": 1,
    "analysis": 1,
    "numeric": 1.3,
    "solve": 1,
    "teardown": null,
    "critical_path": 1.1
  },
  "worst_componentwise_backward_error": 4.1409498780773033e-16
}
```

The example omits platform and configuration metadata for brevity. Consumers
should check `report_type` and `schema_version`, inspect execution outcomes, and
handle `null` and tagged nonfinite values before comparing numeric values.
