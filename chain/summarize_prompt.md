You are summarizing five mechanism-chain runs that have already been written. The runs are finished. Do not search again, and do not add a new mechanism.

Target: {{TARGET}}
Disease: {{DISEASE}}

The five chains follow. Every sentence on a hop comes from that run. Copy each sentence exactly.

{{CHAINS}}

Merge rules:
- Collapse different writings of the same molecule into one name. For example, collapse LDL receptor, LDLR, and low-density lipoprotein receptor into LDL receptor. Put the chosen name in NAMES, and use that name in every later section.
- Do not merge different molecules, such as IL-21 and IL-17.
- Keep direction. A → B and B → A are two edges.
- A hop whose two ends collapse to the same name is a self-loop. Do not write it in AXIS or GRAPH.
- An edge that appears in only one run is still written in GRAPH.
- Do not add a node, an edge, or a sentence that is not in the input.

Write the three sections below. One record per line. Separate fields with a tab. Do not write anything outside these three sections.

NAMES
Chosen name	Other writings, separated by semicolons. Leave the field empty when there is no other writing.

AXIS
Run id	Left	Right	Reference id	Sentence
Keep the original order of each of the five runs. Use Run 1 through Run 5 from the input as the run id.

GRAPH
Left	Right	Runs in which the edge appears, separated by commas	Reference id	Sentence
Keep one row for each directed pair of ends. When the same direction appears in more than one run, join the run ids and keep one of the sentences. Scoring reads only this section.
