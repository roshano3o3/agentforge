# benchmarks/

Results of `agentforge compare --models ... --datasets ...`, one JSON file per
invocation (`<date>-compare-<hash>.json`), in the format of
[`cli/agentforge_cli/benchmark_schema.json`](../cli/agentforge_cli/benchmark_schema.json)
(`agentforge.benchmark/v1`). Each file records the date, every dataset's
version and content hash, the model specs and ids, the prompt's sha256, the
worker's code version, the pricing file's hash, the pre-run cost estimate
and its assumptions, the actual (estimated) spend, and per model and dataset
every run's id, status and metrics, with mean / min / max over the repeats.

[`compare.yaml`](compare.yaml) is the default comparison (`agentforge compare
--plan benchmarks/compare.yaml`): the models in run order, each with its
datasets, repeats and per-case timeout.

Results:

- [`2026-10-08-compare-a365a887.json`](2026-10-08-compare-a365a887.json) -- run
  on 2026-10-07 (US Central; 00:58 UTC on the 8th): scripted v1 (reference),
  Llama 3.1 8B via Ollama (trajectory dataset, 1 run), Claude Haiku 4.5 and
  Claude Sonnet 5.5 (both datasets, 3 repeats). 17 runs, all completed;
  estimated spend $2.02.

A file appears here only from a real run; nothing in this directory is written
by hand.

See the root README, "Benchmarks: real LLM agents".
