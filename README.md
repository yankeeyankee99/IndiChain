# IndiChain

IndiChain nominates indications for a specified molecular target and keeps each mechanistic step inspectable. A freeze date limits literature retrieval and clinical-trial lookup. Nomination writes three closed lists (actions, processes, and products), runs the same prompt as five independent replicates, and aggregates them into one top five. Mechanism search stores literature sentences and labels each step supported or co-occurrence.

## Run

Set `ARK_API_KEY` for model calls. Mechanism search also needs `BOHRIUM_API_KEY` for Bohrium paper search. Put them in the environment or in a `.env` file. Do not commit that file.

```text
python run.py --target Kv1.3 --as-of 2026-04-30
python chain/axis_live.py --target Kv1.3 --mesh D005922 --out out --as-of 2026-04-30
```

Nomination prompts are `nominate/prompt4_lists.md`, `nominate/prompt4.md`, and `nominate/prompt_context.md`. The procedure note is `workflow.md`.

## Scores

`score/bench_percent.py` computes the three percentages reported in the manuscript: absent from the known set, verbatim in the cited abstract, and published on or before the freeze date.

`score/assess.py` and `scoring.md` compute a separate weighted indication-assessment score. That score is not the three percentages in the manuscript.

## Layout

- `run.py` runs one campaign batch.
- `nominate/` writes the closed lists, five nominations, and the top five.
- `chain/axis_live.py` builds and labels a mechanism chain.
- `score/` reads stored outputs.
