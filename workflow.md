# How IndiChain runs

One complete run has three stages. The stages do not share a dialogue.

1. Nomination (`nominate/`): given only a target and a freeze date, write candidate indications.
2. Mechanism chain (`chain/axis_live.py`): given a target and a disease, connect a path from the literature.
3. Scoring (`score/`): read the stored outputs of the first two stages. Do not rerun nomination or the chain.

The chain entry is `chain/axis_live.py`. The nomination entry is `nominate/run_agent_plan.py`. A campaign batch is started with `run.py`.

## Where model calls go

Nomination and the chain are chat completions. Thinking mode is on at start. The program does not pass the previous stage's dialogue, reasoning, or tool record to the next stage.

| Use | Request goes to | Model name |
|---|---|---|
| Catalog nomination, and Kv1.3 Results1 | `https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions` | `deepseek-v4-pro` |
| Kv1.3 Results2 through Results5 | `https://open.bohrium.com/openapi/model-gateway/v1/chat/completions` | `deepseek-v4-pro-0813` |
| Mechanism chain | The Bohr gateway when an access key is set, otherwise the coding endpoint | The code writes `deepseek-v4-pro`. The gateway is sent `deepseek-v4-pro-0813` |

Each chain request carries `temperature: 0` and `thinking: {type: enabled}`. Nomination turns thinking on and does not write a temperature. One nomination reply is capped at 32000 tokens. A nomination that uses tools is not streamed. Tool rounds stop at 8. After round 8 the program tells the model to write the table from the tool results it already has, and not to call a tool again.

At most 8 chain model calls run at once. A 429 is waited out and retried. A 402 stops the run.

## Freeze date

The freeze date is `YYYY-MM-DD`. Kv1.3 uses 2026-04-30.

A trial whose start date is before the freeze date counts as already started. A trial whose start date is the freeze date does not. When nomination looks up clinical trials, the date range ends the day before the freeze date. When the chain looks up literature, the PubMed `maxdate` is the freeze date, and a local filter drops a paper whose publication date is after the freeze date. A paper without a day-level date is kept. When Bohrium returns only a year, the program treats 1 January of that year as the publication date.

The model's own memory is not cut off by the freeze date. The freeze date limits paper retrieval, clinical-trial retrieval, and the instruction that material after the freeze date is unavailable.

## 1. Nomination

### Input

- A target name. The Kv1.3 prompts use `Kv1.3`.
- A freeze date.
- A short biological context. For Kv1.3 the context is already in `nominate/prompt4_lists.md` and `nominate/prompt4.md`: class-switched memory B cells, plasmablasts, and antibody-secreting cells depend on Kv1.3 for immunoglobulin class switching, differentiation toward antibody-secreting cells, and secretion rate; effector-memory T cells, macrophages, and dendritic cells also carry Kv1.3; the dependence changes with differentiation state.
- For another target, `nominate/prompt_context.md` asks the model to write a context. That prompt replaces only the target name. It asks for the cell or molecular states that depend on the target, what those states do when they depend on it, and how the dependence changes with state. One paragraph. No disease name. No later tables. The context is saved as that target's `context.md` and then filled into the two prompts.

The prompts do not receive a pre-specified target indication. The known-indication list is not put into the nomination prompt.

### Step 1. Three closed lists

The prompt file is `nominate/prompt4_lists.md`. One call, no tools.

The model derives three tables. The tables contain no disease names and no proper names of disease-defining autoantibodies or other disease-marker molecules. Only information from before the freeze date is used. The three tables end in this order:

- Actions: effector programs of the cells in which the target acts. Each line is that cell's differentiation, its secretion, or a change it causes on another cell.
- Processes: the biological processes these programs advance, including the cell that carries out the program and the cell that responds.
- Products: the molecular or cellular output classes released while these processes run. When a class has isotypes, each isotype is its own line. The parent class is not used in its place, and an isotype is not split further.

Each line is numbered. The number is a citation label, not a rank of relevance or confidence. The output is English.

The program keeps only the body that contains the three headings and writes `lists.md`. The raw reply, the reasoning, and the request record are written under `lists-run/`.

### Step 2. Five indication lists

The same `lists.md` is filled into `<<<CLOSED_LISTS>>>` of `nominate/prompt4.md`, and the freeze date into `<<<AS_OF>>>`. That prompt is run five times. Each run is its own call. The five runs may go in parallel. The English prompt is `nominate/prompt4.md`. The limits the model receives are:

- The three tables are closed. An item may not be added, removed, renamed, or narrowed, and another table may not be written.
- This is hypothesis generation, not a literature review and not a final ranking of indications. A later step searches the literature to test the target–disease pair.
- Prefer hypotheses that a literature search can test. Breadth comes from different chains in the three tables. Look for indications where the target has a biologically coherent therapeutic reason, especially where the need is clearly unmet. A speculative link is not written as an established fact.
- Whether a disease has already entered a trial in patients is decided only by the return of `clinical_trials` on that call. Before a disease is excluded, look it up with the freeze date, the disease name, and the drug or the target as the intervention. When a drug name is known, look up that drug. Exclude a disease only when the returned trial is of a drug developed for the target, in patients with that disease, and started before the freeze date. Completed, ongoing, and stopped patient trials all count. An empty return or an error keeps the disease and treats it as not yet in the clinic. Do not exclude from memory of a trial, an approval, a review, or a date. A trial id or a start date is written only when the tool returned it. A direct target–disease paper is not required. A new indication may be speculative. Do not keep a known or already clinical indication in order to fill the list. When fewer than 10 diseases pass, keep only those that passed. That rule overrides the later request for 10 to 15. When several diseases need a lookup, call them in the same round.
- Classify each disease by the class in which it is accepted. A primary tumour is kept when the injury follows a listed mechanism.
- Read the three tables as one mechanism. The biological context is the start of the hypothesis. Read that chain first, then the other chains in the tables. An indication is an ongoing injury that follows one listed action, one process that action advances, and one product that process releases. Each disease cites those three items. Do not introduce an action, process, or product from outside the tables. Several diseases may cite the same item. Two names are duplicates only when they use the same table item and the same cell state. Order inside a table is a citation label, not a rank. Diseases under one product do not stand in for diseases under another product. Before closing, read each chain that ends at a different product. Do not add a disease in order to fill the list. Then keep 10 to 15 indications. At most three indications cite the same product.
- Call `mesh` before writing a disease name. The disease name is the preferred term returned by that call, and the MeSH cell is the descriptor id from the same return. Do not write a class or a parenthesis in the disease name. Do not write a name or an id the tool did not return. A name with no descriptor is not kept. On a tool error, do not fill an id from memory.
- Dependence of a table item on the target that has not yet been checked can be a shared hypothesis of every disease that cites it. Pointing out that gap is not a reason to drop those diseases. A disease-specific step outside the tables does not drop the disease, and is not added to the tables, when a listed item drives the disease or changing that item could lessen the ongoing injury. Drop a disease only when no listed item drives it and changing any listed item would not lessen the ongoing injury.
- The proposal may be wide. The statement of fact is strict. Do not invent a citation, an experimental result, a trial id, a drug program, or a development status. Do not invent an efficacy number or an evidence score. The output is English.
- The final table has two columns: Disease and MeSH.

Each reply is stored under `nominate/run-N/`. The file name includes the model name and the run index: the body `.md`, the reasoning `.reasoning.md`, and the full request and tool record `.jsonl`.

### The two tools called during nomination

The program runs the tools. The result comes back as a tool message in the same dialogue.

`clinical_trials` queries ClinicalTrials.gov API v2 (`https://clinicaltrials.gov/api/v2/studies`). The arguments are the freeze date, a disease name (may be empty), and an intervention name (may be empty). The start-date range ends the day before the freeze date. At most 3 pages of 20 studies are read, and at most 15 trials that started before the freeze date are kept. The return has the NCT id, a brief title, the start date, the status, the intervention name, and the disease name. An empty `trials` list means this lookup found no earlier trial. An error is not an empty result.

`mesh` queries NLM MeSH (`https://id.nlm.nih.gov/mesh/lookup`; an entry term is then resolved to a descriptor by SPARQL). Only a name that matches a preferred term or an entry term after spaces and case are normalized is accepted. A near spelling returns an empty descriptor. The return has the descriptor id, the preferred term, and the matched term. An empty descriptor means the name is not a MeSH term. An error is not an empty result.

Both lookups have a local cache. A cache hit does not call the external service again.

### Step 3. Five runs become a top five

Aggregation starts only after all five replies are written. If one is missing, there is no top five. Aggregation reads only the Disease and MeSH table of each reply, not the reasoning.

Within one run, the same MeSH descriptor is counted once. The disease name is normalized for spaces and case, then looked up in MeSH. When the name matches the preferred term or an entry term of exactly one descriptor, that id is used and the disease name becomes the preferred term. When it matches none or more than one, the id the model wrote is not used, and the wording stays on its own row. Across the five runs, the same descriptor id is one row. The count is the number of runs, out of five, in which that row appears. Writing it twice in one run still adds 1.

The full table is ordered from the highest count to the lowest. Equal counts are ordered alphabetically by disease name, ignoring case. That alphabetical order is not a second score. The top five are the first five rows in that order that have a descriptor id. A row without an id stays in the frequency table, does not take a top-five place, and the next row fills the place. When fewer than five rows have an id, the top five contains only those rows. A disease tied with row 5 but placed at row 6 or later is not written into `top5.md`.

The files written are:

- `nominate/counts.md` and `nominate/frequency.md`: every disease and its count.
- `nominate/top5.md`: `rank. Preferred term (descriptor id) x/5`.
- `nominate/top5.json`: the same top five, for the next stage to read.

## 2. Mechanism chain

One run is one target plus one disease. The entry is:

```text
python chain/axis_live.py --target TARGET --mesh MESH_ID --out OUTPUT_DIR --as-of YYYY-MM-DD
```

When `--as-of` is omitted, the program uses 2026-04-30. The disease end of the task is the pathology, not a drug name or a phenotypic slogan. For IgA nephropathy the MeSH descriptor is `D005922`.

### Input

- A target string, for example `Kv1.3`.
- The disease's MeSH descriptor id. The user does not also type the disease name. The program asks MeSH SPARQL for the preferred term and the entry terms of that id.
- The freeze date.

The program then queries UniProt REST (`https://rest.uniprot.org/uniprotkb/search`) for the human record: first an exact gene name that is reviewed, then a gene name that is reviewed, then a protein name that is reviewed, and last a record that need not be reviewed. The return used later is the primary accession, the gene name, gene synonyms, the recommended protein name, and the short name. Those names decide which target-side sentences are kept. The accession is not used to keep a sentence.

### Where the literature comes from, and where it stays

The model first writes two English retrieval questions (`TASK questions`). The target question asks for the target's actions, processes, and products. The disease question asks for the pathology. The format is `QUESTION`, a tab, `target` or `disease`, a tab, and the question. If either question is missing, the run stops.

Disease aliases are a separate question (`TASK lexicon`). The input is the MeSH preferred term and the entry terms. The model adds abbreviations, aliases, and common names used for the same disease in papers, one `TERM`, a tab, and the name per line. If there is no such line, the question is asked again. These names are merged with the MeSH terms and used to keep disease-side sentences.

Both sides are sent to Bohrium paper search: `https://open.bohrium.com/openapi/v2/paper/rag/pass/keyword`. On the target side, `words` is only the target name and `question` is the target question. On the disease side, `words` is only the MeSH preferred term and `question` is the disease question. Aliases are not added as a further query. Each side keeps at most 500 papers. A paper published after the freeze date is dropped. A paper without a day-level date is kept.

Sentences are cut from abstracts only. The title is not matched. A sentence that contains Chinese characters is dropped. A target-side sentence is kept when it contains a UniProt gene name or protein name. An accession does not count. A disease-side sentence is kept when it contains a MeSH term or an alias the model added. Names match on word boundaries: `IgA` does not hit because it occurs inside `IgAN`. The same paper and the same sentence are kept once. A paper that does not match is not saved as an abstract.

The run writes `sentences.json` (each sentence has a date, a title, the sentence, the side, and the doi, pmid, and paperId when they exist) and `lexicon.json`. If either side has 0 papers or 0 sentences, the run stops. When both files already exist, a later run reads them and does not search again.

### At most 10 nodes on each side

Sentences are handed to the model in batches of about 8000 characters. Batches may run in parallel.

The target-side prompt (`TASK ends`, kind `target action`) asks the model to copy a downstream action, process, or product that appears in one sentence, as those exact words, at most 10 lines. Fewer is correct. The disease-side prompt (kind `disease pathology`) asks for a pathological cell, process, or molecule on the same rule.

The program keeps a name only when the whole line is a continuous span of one sentence. A line that is not a continuous span is skipped, and that batch continues. A parenthesis and the text inside it at the end of a name are removed when the name is stored. A parenthesis in the middle is kept. A line identical to the target name or the disease name is dropped.

Each kept span is then named once (`TASK name`). That question receives the sentence and the copied span. It does not receive the target name or the disease name. A node is an object, or a short process name with no preposition. If a process takes an object through of, to, in, for, by, with, or from, the object is written and the process word and the place are not kept. If the span names a substrate and a product, the product is written. If the span is already that object or short process name, the same words are written. A parenthetical abbreviation in the middle, or a space beside a symbol, may be deleted. No other words are added. One line.

The program checks the written name. If the name has more words than the span, or it newly contains a target-class word or a disease word that was not in the span, the program falls back to the span. A phrase with of, to, in, for, by, with, or from is dropped. The name must still be a continuous span of that sentence. The only edits allowed are deleting a parenthetical abbreviation in the middle, or removing a space before a symbol (`CD4 +` may become `CD4+`). Words that occur in order but are not continuous do not pass. The program does not treat a hyphen and a space, or a Greek letter and its English spelling, as the same. After this check, each side keeps at most 10 names. Above 10, `TASK merge` asks the model to repeat existing names only, with no new name, and the list is cut back to 10.

The two ends are written to `ends.json`. If a reply cannot be parsed while the ends are being selected, the part already selected is written to `ends-partial.json`, the reply to `ends-reply.txt`, and the run stops.

### Extra sentences at the two ends

After the node names are fixed, each target-side node and each disease-side node gets one PubMed search. The query is a title-or-abstract AND of the two names:

`("<anchor>"[tiab]) AND ("<node>"[tiab])`

The anchor on the target side is the input target name. The anchor on the disease side is the MeSH preferred term. At most 50 papers are taken. A sentence is kept only when both names occur in it on word boundaries. The sentence originally copied from the Bohrium abstract is placed first, PubMed sentences follow, duplicates are removed, and the total is at most 10 sentences. When PubMed returns no sentence or the search fails, the original sentence is kept. Intermediate steps are not supplemented here. This step does not judge direction.

### Walking from a target-side node to a pathology node

Each target-side node opens its own dialogue. At most 4 branches walk at once. The start of the dialogue states the target, the disease, the start of this branch, and the pathology nodes already read, and states that the task is a mechanism from the target to the disease. A later branch does not see an earlier branch's dialogue.

Each hop asks the model for the next node: at most 3 next nodes that head toward the pathology nodes. A node is an object or a short process name with no preposition. A process phrase that takes an object through of, to, in, for, by, with, or from is not proposed. A change in amount or function is not named. Fewer is correct. Names already on this path, and the target, are not repeated.

At most 3 names are kept. A name already on the path, the target name, an empty name, and a phrase with one of those prepositions are dropped.

Each proposed name is searched with the current node, and with the writings of each, as a PubMed AND query. The rule is the same title-or-abstract search as above. A sentence is kept only when both writings occur in it, at most 10 sentences. If no such sentence exists, the branch stops and does not continue. If sentences exist, the node and the sentences are written into the dialogue and the walk continues. A path has at most 6 intermediate nodes. The next hop stops. When a proposed name is identical to a listed pathology node, ignoring case and accepting the parenthetical writing of the same node, the branch is complete.

The walk does not judge whether a sentence states left-to-right. Direction at this stage is the order of the proposal. Two names in the same sentence are enough to connect the step, including when the narrative order in the sentence is the reverse of the path.

A complete axis is saved as: target → first node → … → pathology node → disease preferred term. The sentences from the target to the first node are the supplemented sentences of that target-side node. The sentences from the pathology node to the disease name are the supplemented sentences of that pathology node. Intermediate hops use the sentences kept during the walk.

### Chain output

After every target-side node has been walked, if at least one axis is complete, the model reads those axes (`TASK status`) and writes, for each step, what the retrieved sentence supports and where it does not carry the step. That question does not use a fixed list.

The files written are:

- `axes.json`: `axes` are the complete axes. Each step has `left`, `right`, and `sentences` (sentence, doi, pmid, paperId, date, title). `incomplete` holds paths that did not connect and the reason they stopped. `status` is the reading note above.
- `report.md` and `report.html`, generated from those axes.

This step does not automatically label sentence direction. The direction label is a separate run: `python chain/axis_live.py --label RESULT_DIR`. It only reads sentences already saved. It does not search again. Each sentence is asked on its own:

```text
Left: <left>
Right: <right>
Sentence: <sentence>

Does this sentence state that the left leads to the right?
Answer with one word: supported or co-occurrence.
supported: the sentence states that the left leads to the right, including a paraphrase of that direction.
co-occurrence: the two only appear together, are listed as parallel readouts, one is only a biomarker or measurement of the other, or the stated direction is the opposite.
Do not add a direction the sentence does not state.
```

Only the first word is used. The first word supported is recorded as supported. Any other reply is co-occurrence. The same pair of nodes and the same sentence are asked once. If any sentence on a step is supported, the step is supported. If the step has sentences and none is supported, the step is co-occurrence. The label is written back to `relation` in `axes.json`, and the report is rewritten. A directory with no complete axis is skipped.

### Limits on the mechanism chain

| Item | Limit |
|---|---|
| Bohrium papers per side | 500 |
| Nodes per side | 10 |
| End-selection batch | about 8000 characters |
| Next nodes proposed at one hop | 3 |
| PubMed papers between two names | 50 |
| Sentences kept on one hop | 10 |
| Intermediate nodes on one path | 6 |
| Axes walked at once | 4 |
| Model calls at once | 8 |

## 3. Scoring

The weighted score is in `scoring.md`. Scoring only reads results. The program nomination reads the five disease names in `top5.md` and uses a MeSH id already on the line. The program chain reads `hops` of one complete axis in `axes.json`. The report labels supported and co-occurrence are not part of that weighted score.

The three percentages reported in the manuscript are computed by `score/bench_percent.py`.

## Boundaries to keep when writing Methods

- Nomination does not know which disease will later be selected. The top five is a count across runs. It is not a ranking the model gave in one reply, and it is not a second score besides alphabetical order for ties.
- When nomination excludes a known clinical indication, it accepts only the return of the clinical-trial tool on that call. It does not search papers, and it does not use a review to exclude.
- Literature sentences in the chain come from Bohrium abstracts and from PubMed title and abstract fields. The title is not used to decide whether a sentence contains a name. A paper without an exact date is kept.
- While the chain is assembled, two names in one sentence are enough to connect. Left-to-right is the later direction label and a scoring rule. It is not a filter at assembly. The direction label does not enter the weighted score.
- Stepwise evidential support in the weighted score looks at whether an opened PubMed title or abstract states the direction. It does not look at whether the stored sentence was labelled supported, and it does not require that sentence to appear verbatim in the title or abstract.
- Scoring does not rewrite the nomination table or the chain.
