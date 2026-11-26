# Scoring

`score/assess.py` compares two deliverables with one formula: the program, and an unaided model. Nomination and the mechanism chain are scored separately and then added. Weights are fixed before scoring. When one item is 0, the others are still scored by their own rules.

The program's nomination deliverable is the top five aggregated from five runs of the same task. The unaided model's nomination deliverable is the disease names in one reply. The program's chain deliverable is one path from the specified target to the specified disease. The unaided model's chain deliverable is one path in that same reply.

Both sides use the freeze date of that program run. For Kv1.3 the freeze date is 2026-04-30.

The three percentages in the manuscript (absent from the known set, verbatim in the cited abstract, and published on or before the freeze date) are computed by `score/bench_percent.py`. They are not this weighted score.

## Verbatim in the cited abstract, and published on or before the freeze date

`score/bench_percent.py` counts every sentence stored on the chain. For the program, those sentences are the ones the chain report lists as related, denied, or co-occurrence, including the hop that joins the last node to the disease. For an unaided reply, they are the sentence lines parsed from that reply.

Verbatim in the cited abstract is the fraction whose citation identifies one paper and that are a continuous span of that paper's abstract. Published on or before the freeze date is the fraction of the same sentences whose citation identifies one paper published on or before the pair's freeze date. A sentence that does not meet a test remains in that test's denominator.

A citation identifies one paper when the DOI, PMID, or PMCID stored on that sentence matches one record, or when the author, title, journal, year, and page written on that sentence return exactly one PubMed record. A journal name and a year, with no author and no title, do not identify a paper. Each sentence is resolved from the citation stored on that sentence.

The abstract comparison is `judge_sentence`. The date comparison is `on_or_before`. Reported values are arithmetic means of the per-pair percentages. Kv1.3–IgA nephropathy is omitted from that mean.

## Names used in the manuscript

The rules, symbols, and names above stay as written here. The manuscript uses the names in this table when it refers to this score.

| Name here | Name in the manuscript | One-sentence definition |
|---|---|---|
| Total | Indication assessment score | The five nomination items and two chain items added at fixed weights |
| Mechanistic match | Mechanistic concordance | The ongoing injury of the proposed disease points the same way as a step the target can change |
| Disease name maps to MeSH | Disease-term resolvability | The disease name matches exactly one controlled disease term |
| Novelty | Indication novelty | The fraction of the top five that lies outside the known-indication set at the freeze date |
| No direct refutation | Absence of direct refutation | No direct evidence from before the freeze date shows that the proposal fails |
| Disease names do not contain one another | Indication non-nesting | The fraction of the top five that is not a parent or a child of another name in the five |
| Every step has supporting evidence | Stepwise evidential support | The fraction of steps whose title or abstract states the left-to-right relation |
| The cited paper exists | Reference resolvability | The fraction of step citations that match exactly one real paper |

The five nomination items together are the nomination assessment. The two chain items together are the mechanism-chain assessment. Disease-term resolvability and reference resolvability mean only that the name matches one existing record. They do not mean that the record supports the claim. Indication novelty is the fraction of new indications in the top five, not a subjective score of creativity. Absence of direct refutation means only that no direct refutation was found. It does not mean the claim has been shown.

## Total

\[
Score=10\bar{m}+5\bar{q}+25\bar{k}+5\bar{r}+5\bar{g}+20\,e\,\bar{s}+30\,e\,\rho
\]

The maximum is 100. Nomination contributes at most 50. The mechanism chain contributes at most 50.

| Symbol | Meaning | Range | Maximum contribution |
|---|---|---|---:|
| \(\bar{m}\) | Mechanistic concordance across five positions | 0 to 1 | 10 |
| \(\bar{q}\) | Disease-term resolvability across five positions | 0 to 1 | 5 |
| \(\bar{k}\) | Fraction of the top five that are new indications | 0 to 1 | 25 |
| \(\bar{r}\) | Fraction of five positions with no direct refutation | 0 to 1 | 5 |
| \(\bar{g}\) | Fraction of five positions that do not nest | 0 to 1 | 5 |
| \(e\) | Whether the path runs from the specified target to the specified disease | 0 or 1 | Multiplies the two chain items |
| \(\bar{s}\) | Fraction of steps with evidential support | 0 to 1 | 20 |
| \(\rho\) | Fraction of real citations on the path | 0 to 1 | 30 |

\(\bar{k}\), \(\bar{g}\), mechanistic concordance, the disease name, and direct refutation use the same five top-five positions. An empty position has \(k=0\) and \(g=0\).

Missing material scores 0 and stays in the denominator. Fewer than five diseases, a step with no paper, or a paper that does not open does not remove that position from the denominator.

## The two tasks

The unaided nomination task is the English text below. Only the target name and the freeze date are replaced. The prompt does not define "new" operationally, does not give a known-indication list, and does not give the closed lists.

```text
Target: Kv1.3. Use only information available on or before 2026-04-30. Treat later approvals, trials, reviews, and findings as not yet available.

Find new therapeutic indications for this target. List 5 specific diseases, ordered from the one you would test first to the one you would test fifth. Use English disease names. Put each disease on its own line:

1. Disease name
2. Disease name
3. Disease name
4. Disease name
5. Disease name
```

The unaided chain task is the English text below. Only the target, the disease, and the freeze date are replaced. The model writes every path it considers possible. Each path starts at the target and ends at the disease. A blank line separates paths. Each step is one line. The prompt does not define what counts as real or as support, and it does not give the closed lists.

```text
Target: Kv1.3. Disease: IgA nephropathy. Use only information available on or before 2026-04-30. Treat later approvals, trials, reviews, and findings as not yet available.

Write every mechanistic path you consider possible from this target to this disease. Start each path at the target and end it at the disease. Each step moves from one entity to the next. For each step, give one sentence from the literature and its citation. If a step has no literature, write none. Use English. Put each step on its own line, and separate paths with a blank line:

Left → Right | Sentence | Citation or none
```

The path end used in scoring is the disease name in the prompt. That disease's MeSH preferred term is the same end.

The program runs its existing nomination and chain procedure. The prompts are not rewritten to fit this formula.

Scoring only reads results. It does not change them. The program nomination is the five disease names in `top5.md`. A MeSH id already on the line is used as written. The unaided nomination is the five English disease names in the reply, matched to MeSH. The reply does not have to carry ids. When a run table contains a biological rationale, mechanistic concordance reads the injury and the direction written there. When it does not, the direction is judged from the injury type of the disease name. The program chain reads `hops` of one complete axis in `axes.json`: `left` and `right` of each step, and `sentence`, `doi`, and `pmid` inside `sentences`. The unaided chain reads the step lines required by the prompt: left, right, sentence, and citation or none. The labels supported and co-occurrence in a report, and the earlier labels Supported and Not carried, are not used in the score. How the direction label is asked, and how it is aggregated, is below. Known indications are written in the scoring set. The Kv1.3 set for 2026-04-30 is below.

The gap between the two sides shows up in indication novelty, whether names contain one another, whether a citation opens, and whether a title or abstract states the step. Mechanistic concordance and absence of direct refutation use the same standard and can both be full. Stepwise evidential support asks whether the title or abstract states the direction. It does not require the submitted sentence to appear verbatim, and more steps do not add points.

## How the program nomination becomes a top five

This section applies only to the program's five nominations. The unaided model does not have five runs. The five disease names in its one reply are the five nomination positions. The x/5 on the table is a run count, not a novelty score.

Aggregation starts only after all five runs have written a reply. If any run is missing, there is no top five. Each run contributes only the Disease and MeSH table in that reply. Reasoning is not read.

Within one run, the same MeSH descriptor is counted once. The disease name is normalized for spaces and case, then looked up in MeSH. When the name matches the preferred term or an entry term of exactly one descriptor, that descriptor id is used and the row name becomes the preferred term. When the name matches none or more than one, the id the model wrote is not used, and the normalized wording stays on its own row. Within one run, the same id, or the same unnumbered wording, is counted once.

Across the five runs, the same descriptor id is one row. The count is the number of runs, out of five, in which that row appears. Writing it twice in one run still adds 1. A row that does not resolve to a unique id stays in the frequency table and does not enter the top five.

The full table is ordered from the highest count to the lowest. Equal counts are ordered alphabetically by disease name, ignoring case. The top five are the first five rows in that order that have a descriptor id. A row without an id does not take a place. The next row fills it. When fewer than five rows have an id, the top five contains only those rows.

Each line of `top5.md` is `rank. Preferred term (descriptor id) x/5`. Scoring reads these five disease names and uses an id already on the line. When equal-count rows were written into `top5.md` in another order, scoring keeps that file's order and does not restore alphabetical order. A lower count is not placed ahead of a higher count. A disease at 4/5 therefore enters the top five only when fewer than five numbered rows are at 5/5, and it is chosen among the 4/5 rows in the order written at the time.

## Five nomination positions

Mechanistic concordance, the disease name, direct refutation, and non-nesting use the same five positions. The denominator is fixed at 5.

When the names are numbered or explicitly ordered, the first five are used. When there is no order and there are more than five names, the denominator for these items becomes the number of all disease names. When there are fewer than five, an empty position scores 0 and the denominator stays 5.

The second time the same MeSH descriptor appears, that position scores 0 on all five items. When neither name matches MeSH, identical strings count as a repeat.

## Mechanistic concordance

Position \(i\) has \(m_i=1\) only when all three hold:

- The name is a disease.
- The written ongoing injury passes through a cell state or a product that the target has a reason to change.
- The therapeutic direction agrees with that injury.

Otherwise \(m_i=0\).

\[
\bar{m}=\frac{1}{5}\sum_{i=1}^{5}m_i
\]

When there is no order and there are more than five names, the denominator becomes the number of all disease names.

A speculative link whose direction is coherent scores 1. Nomination does not require a paper. When one overall mechanism states the injury and the direction for a disease, that disease can score 1. When the list gives only a disease name and no injury, the direction is judged from the injury type of that disease.

These positions score 0: an empty position; a repeated disease; a line that only says the disease is common or unmet; a mechanism that is about a different disease; a written product that does not meet the injury; inhibition of the target would worsen the disease. When two diseases are written in one name, the position scores 1 only if both fit. Whether a disease is neurological does not change \(m_i\).

## Disease name maps to MeSH

\[
\bar{q}=\frac{1}{5}\sum_{i=1}^{5}q_i
\]

The denominator rule is the same as for mechanistic concordance.

\(q_i=1\) means the name matches exactly one MeSH descriptor. A preferred term, a synonym, or a Chinese name that matches exactly one descriptor all count. Scoring looks the name up in MeSH. The answer does not have to carry an id.

These score 0: no descriptor; two descriptors fit equally well; only a parent class such as "autoimmune disease"; only a supplementary-concept record; an invented disease name; a repeated position. A spelling that still identifies exactly one descriptor scores 1. When the name matches and the id written with it is wrong, the name scores 1. When only an id is given, that id must exist.

## Novelty

\[
\bar{k}=\frac{1}{5}\sum_{i=1}^{5}k_i,\qquad S_{new}=25\bar{k}
\]

\(k_i=1\) means that the disease is not yet a known indication of this target. A disease is already an indication when a patient trial of a drug developed for that target, in that disease, started before the freeze date. Completed, ongoing, and stopped patient trials count. The trial that starts on the freeze date does not. A healthy-volunteer trial does not. A disease named only in a condition field, and not in the trial title, does not. A drug developed for another target that also acts on this target does not make its indications known indications of this target.

A name that denotes that same disease, including a relapsed, refractory, or phenotype-qualified form, is that indication. A different disease is new. The program counts a name as known when its MeSH id is the id stored for that disease, when its normalized text equals a stored name or alias, or when a stored name or alias appears as a whole phrase inside it. A synonym that does not contain the stored name is known only when that wording is itself stored as an alias. Descendant MeSH descriptors are not added. A review, a marketing authorization, an animal experiment, a case report, or a preclinical announcement does not by itself make the indication known.

One known indication removes only its own share of the five. It does not zero the other new indications. When all five are known, \(\bar{k}=0\). When this target has no known-indication set, indication novelty is not scored 0 and is not scored full. The item stays unscored.

The known set is written before the unaided model's list is seen, and it is not expanded afterwards. For Kv1.3 at the freeze date 2026-04-30 the set is two patient trials: dalazatide (ShK-186) in plaque psoriasis, NCT02435342, and YR001 (PA032) in atopic dermatitis, NCT06309355. A nominated disease that is neither of those two, including IgA nephropathy and multiple sclerosis, is new.

Nomination and scoring use this same set. A disease is removed at nomination only when that call to `clinical_trials` returns a patient trial of a drug developed for the target that started before the freeze date. An empty return or an error keeps the disease. A review does not add an indication at nomination or at scoring.

## Absence of direct refutation

\[
\bar{r}=\frac{1}{5}\sum_{i=1}^{5}r_i
\]

The denominator rule is the same as for mechanistic concordance.

\(r_i=0\) only when there is a direct refutation from before the freeze date. A direct refutation is any of the following:

- A trial of a drug developed for the target, in patients with the disease, ended for lack of efficacy.
- Available material shows that the written product does not take part in the injury.
- Available material shows that inhibiting the target would worsen the disease.

No paper, a theoretical doubt, a trial stopped for safety or recruitment, a failure of a drug against another target, and a remark in the reply that the proposal "might not work" all keep \(r_i=1\). A known indication is handled by novelty and is not scored 0 here. The refutation must be about that disease. A failure that appears only after the freeze date is not counted. An empty position and a repeated disease score 0.

## Disease names do not contain one another

\[
\bar{g}=\frac{1}{5}\sum_{i=1}^{5}g_i
\]

The denominator rule is the same as for mechanistic concordance.

\(g_i=0\) only when this name is a parent or a child of another of the five. The parent scores 0. The more specific name can still score 1. When both are specific diseases and neither contains the other, both score 1. An empty position and a repeated disease score 0. When it is unclear which contains which, both score 1.

## Which chain path is scored

\(e=1\) means the path starts at the specified target and ends at the specified disease. The disease name in the prompt and that disease's MeSH preferred term are the same end. When the start or the end does not match, \(e=0\), and both stepwise evidential support and reference resolvability are 0.

The program selects the complete axis in `axes.json` with the highest \(\bar{s}\). If \(\bar{s}\) is tied, it selects the higher \(\rho\). If that is also tied, it selects the earlier axis in the file. The unaided model uses the same rule among the paths it wrote from the specified target to the specified disease. Both chain items are computed on that path.

Retrieval records that are not on this path do not enter \(\bar{s}\) or \(\rho\).

## Every step has supporting evidence

When the path has \(n\) steps, \(n\ge 1\), and \(e=1\):

\[
\bar{s}=\frac{1}{n}\sum_{j=1}^{n}s_j
\]

\(s_j=1\) means at least one opened PubMed record has a title or abstract that states the left-to-right relation of this step. A paraphrase of the same relation in the abstract scores 1. Co-occurrence, the opposite direction, and two items only listed together on the same pathway are \(s_j=0\).

When there is no path, the model refuses, or \(e=0\), this item is not divided. It is 0.

When a step has more than one paper, one real paper whose direction holds is enough for \(s_j=1\). Only a fabricated paper, only the model's own statement, or a title and abstract that do not state the relation all score 0. A submitted sentence that is not verbatim does not by itself make \(s_j=0\). When the title and abstract do not state the relation, the step scores 0 even if the sentence might be in the full text. For an ordinary molecular step, a title or abstract that states the direction is enough. When the step is written as a cause specific to that disease, the title or abstract has to land on that disease or a related tissue. The title and the abstract are the text that counts.

An unsupported step stays in the denominator. Removing such a step from the path raises the fraction only when every remaining step itself holds. A step that jumps from the target directly to the disease scores \(s_j=0\) when the title and abstract do not state that relation directly.

## Direction labels on the program chain

While the program builds a chain, each stored sentence is asked once, with `deepseek-v4-pro`, whether the sentence states left-to-right. That question is not scoring. The prompt is:

```text
Left: {left}
Right: {right}
Sentence: {sentence}

Does this sentence state that the left leads to the right?
Answer with one word: supported or co-occurrence.
supported: the sentence states that the left leads to the right, including a paraphrase of that direction.
co-occurrence: the two only appear together, are listed as parallel readouts, one is only a biomarker or measurement of the other, or the stated direction is the opposite.
Do not add a direction the sentence does not state.
```

The model is responsible only for that one word. The first word of the reply is taken, surrounding punctuation is removed, and case is ignored. When the first word is exactly supported, the sentence is supported. Every other reply, including an empty reply and a multi-word explanation, is co-occurrence. The same left, right, and sentence are asked once, and the result is reused. An empty sentence is not sent.

The step label is aggregated by rule. If any sentence on the step is supported, the step is supported. If the step has sentences and none is supported, the step is co-occurrence. The label is written on `relation` of each sentence and each step in `axes.json`, and it appears in the report.

Scoring does not read `relation`. \(s_j\) is recorded only from whether an opened PubMed title and abstract state left-to-right. A step the report marks supported still has \(s_j=0\) when the title and abstract do not state that direction.

## The cited paper exists

When \(e=1\) and the path has steps:

\[
\rho=\frac{R}{U+E}
\]

\(R\) is the number of citations attached to steps that match exactly one PubMed record. The same paper written twice on the same step counts once. \(U\) is the number of citations attached to steps, including fabricated citations and citations that do not open. \(E\) is the number of steps with no citation. When \(U+E=0\), \(\rho=0\).

A chain with no papers therefore has \(U=0\), \(E=n\), and \(\rho=0\).

A real paper whose content does not support the step still counts in \(R\), and that step has \(s_j=0\). These do not count in \(R\) and stay in \(U\): an identifier and a title that are not the same paper; an author and year that match several papers or none; only an internal id that does not open. An identifier that opens after spaces are removed counts in \(R\). One that is close but still does not open after that cleanup does not count in \(R\).

A review, and a preprint that has entered PubMed, counts in \(R\) when it opens as exactly one record. A retracted paper counts in \(R\). A trial registration, a patent, and a web page that are not in PubMed do not count in \(R\). The title and abstract are taken from that PubMed record. Support for the step is judged only from the title and abstract that can be read. When they cannot be read, that paper does not make \(s_j=1\). A real paper published after the freeze date counts in \(R\). When a program output has an internal paperId and also a DOI or PMID, whether the DOI or PMID opens in PubMed is what counts.

A reference list that is not attached to any step does not enter \(R\), \(U\), or \(E\). When the reply states that a step has no paper, and that step is on the scored path, the step counts in \(E\).

## Order of scoring

1. Write the known-indication set for the target at the freeze date.
2. Fix the five nomination positions and compute \(\bar{m}\), \(\bar{q}\), \(\bar{k}\), \(\bar{r}\), and \(\bar{g}\).
3. Fix the scored path. When \(e=0\), both chain items are 0. When \(e=1\), compute \(\bar{s}\) and \(\rho\).
4. Substitute into the total formula.

After scoring, the weights are not changed, and a disease already marked new is not reclassified as known.
