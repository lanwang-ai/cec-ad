# CEC-AD: Claim–Evidence Consistency Benchmark for Agent Deliverables

Code and de-identified data for the paper *Claim–Evidence Consistency for Agent Deliverables: A Benchmark and Measurement Framework* (NeurIPS 2027 Evaluations & Datasets submission).

## What this is

The benchmark asks whether each result claim in an LLM agent's natural-language report is supported by the **execution record of the same run**, with five states: supported (S), partially supported (P), no evidence (N), contradicted (C), and not scored (L). The judgment order is L → N → C → S → P (conflict dominates partial matches), with an evidence-relevance gate that prevents unrelated tool errors from being counted as contradictions.

- **Corpus**: 89 captured sessions / 6,366 claims (83 unique session identifiers; six IDs appear in two workspaces).
- **Gold standard**: 359 claims annotated by three independent annotators (Fleiss' κ = 0.628); 349 majority-vote labels + 10 adjudicated 1-1-1 splits.
- **Baselines**: deterministic rules (default engine 27.9% agreement on 359; minimal-capture rule judge 31.8% on the held-out test split), a keyword heuristic (29.6%), an LLM judge (23.1%, qwen-plus), a constant-P prior (45.3%), and the revised rule judge v2 (63.1%, the main baseline).

## Quickstart

```bash
cd cec-ad
python -m pip install -r requirements.txt
python code/repro/run_all.py            # reproduces the main comparison table (no API key needed)
python code/judges/test_rule_v2.py      # unit tests for the rule v2 judge
```

Expected `run_all.py` output on the **released (strongly de-identified) data** (test split, n = 179): rule judge 54/179 = 30.2%; heuristic 51/179 = 28.5%; rule v2 113/179 = 63.1%; constant-P 81/179 = 45.3%. The paper reports numbers computed on the internal text (31.8 / 29.6 / 63.1 / 45.3); the difference comes from path masking and is documented in `data/release_variant_numbers.md` and `data/release_variant_numbers.md`.

## Repository layout

```
data/annotation/        gold-standard items, split, raw annotation workbooks
code/judges/            default rule judge, heuristic, rule v2 judge, LLM adapter
code/audit/             evidence engine (default behavior), trace parser
code/annotation_tools/  sampling, statistics, adjudication, collection checks
code/repro/             reproduction script for the main comparison table
manual/                 annotation manual (v2) and second-round protocol
docs/limitations.md     error structure and limitations (paper §6)
results/                paper result tables and the v2 rerun JSON
```

## Optional: LLM judge reproduction

`code/judges/llm_judge.py` supports several providers. It reads API keys from `%TEMP%/<REPO>/keys/{dashscope,ark,zhipu,deepseek,moonshot}.txt` (this local convention is kept for reproducibility; in public use, set the corresponding environment variables). The main results do **not** require any LLM call.

## De-identification

Absolute paths are replaced by `<ROOT>`/`<HOME>`/`<SYSTEM>`, emails/phones/tokens are masked, and annotator identities are only A/B/C. Sessions containing sensitive personal information or credentials were excluded.

## License

Code: MIT (see `LICENSE`); data: CC BY 4.0 (see `data/data_card.md`). The final licenses will be confirmed at camera-ready.

## Citation

BibTeX entry: to be added at camera-ready. During review this repository is anonymous.
