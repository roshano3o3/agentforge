# benchmarks/

Results of `agentforge compare --models ... --datasets ...`, one JSON file per
invocation (`<date>-compare-<hash>.json`), in the format of
[`cli/agentforge_cli/benchmark_schema.json`](../cli/agentforge_cli/benchmark_schema.json)
(`agentforge.benchmark/v1`). Each file records the date, every dataset's
version and content hash, the model specs and ids, the prompt's sha256, the
worker's code version, the pricing file's hash, the pre-run cost estimate
and its assumptions, the actual (estimated) spend, and per model and dataset
every run's id, status and metrics, with mean / min / max over the repeats.

**No results are committed yet.** No paid model has been run (Benchmarks part
A built the harness only), and Ollama wasn't installed on the machine where
part A was built, so there is no local-model result either. A file appears
here only from a real run; nothing in this directory is written by hand.

See the root README, "Benchmarks: real LLM agents".
