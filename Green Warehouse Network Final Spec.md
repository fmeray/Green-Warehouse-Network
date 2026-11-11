# Green Warehouse Network: Final Spec

Nov 11, 2026 · @fatma

A tool that chooses warehouse sites with a primary and a backup for every customer, minimizes CO₂ or cost, and lets the user explore what-ifs in chat. This is the simplified version: feature changes stop here, only bug fixes and the acceptance criteria remain.

## 1. Goal and success criteria

**Decide how many warehouses to open, where to put them, and which two serve each customer (a primary and a backup), so that the objective the user selects (total CO₂ or total cost) is minimized, the other is reported alongside it, and no single warehouse failure leaves a customer unserved.**

The tool answers five questions:

1. How many warehouses are needed (at least 2, because every customer needs a backup)?
2. Where should they be?
3. Which warehouse is each customer's primary, and which is the backup?
4. What does resilience cost in CO₂ and money compared with one center-of-gravity warehouse?
5. How do the answers change when the user alters inputs (a site closes, demand grows, trucks go electric)?

Success means:

- Every customer has a primary and a backup, and they are different open sites.
- If any one warehouse fails, the others absorb its demand within capacity.
- A user describes a scenario in plain English in a chat and gets a solved result in under 60 seconds, with a clear list of what changed since the previous run.
- The tool reports the cost of avoiding CO₂: the extra € per tonne of CO₂ saved when switching from the cost-optimal to the CO₂-optimal network.

## 2. Scope and assumptions

A static, single-period facility location study for the German market, with a conversational scenario layer. It uses no live operational data.

| In scope | Out of scope |
| --- | --- |
| Warehouse count, location and customer assignment | Vehicle routing and last-mile scheduling |
| Primary plus backup for every customer | Inventory and stock levels |
| CO₂ and cost from transport plus warehouse operation | Multi-period or dynamic network design |
| Single-site failure resilience | Several sites failing at once |
| Plain-English what-if scenarios in a chat | Live data, email delivery, n8n, MCP server, web API |

Assumptions:

- Customers and candidate sites are inside Germany. Locations and demand are static, read from the files in data/.
- Transport CO₂ = distance × load × emission factor (g CO₂ per tonne-km). Transport cost = distance × load × cost rate (€ per tonne-km). Distance is road distance.
- Each open warehouse has a fixed cost and a fixed CO₂ footprint per year.
- A backup is a second open warehouse within a maximum distance. If the primary fails, the backup takes the customer's whole demand.
- The single center-of-gravity warehouse is a comparison baseline only, because it cannot meet the backup rule.

## 3. Data

Four static inputs in the data/ folder feed the model. The default data has 24 customers and 12 candidate sites; the model and the validation rules must still handle up to 60 customers and 15 sites. The files in data/ are the single source of truth: the app reads them and never regenerates them.

| Dataset | Fields | Source | Notes |
| --- | --- | --- | --- |
| customers.csv | id, name, lat, lon, demand (tonnes/year) | Built by hand, stored in data/ | Demand must be positive |
| candidate\_sites.csv | id, name, lat, lon, capacity (tonnes/year), fixed\_cost (€/year), fixed\_co2 (t CO₂/year) | Built by hand: German logistics hubs or major cities | Capacity totals at least 2× total demand. Fixed CO₂ and fixed cost are illustrative, not measured |
| emission\_factors.csv | mode (diesel, electric), g CO₂e per tonne-km, source, region, fetched\_at | Climatiq API, fetched once; a published GLEC or UBA value as fallback | Defaults the user can override |
| distance\_matrix.csv | customer × site road distance in km | OpenRouteService Matrix API (driving-hgv), fetched once | Cached, so the solver never calls an API |

Emissions for customer i served from site j: e\[i\]\[j\] = distance\[i\]\[j\] × demand\[i\] × factor ÷ 1,000,000 (t CO₂).

**One-time fetch scripts.** The emission factor script calls Climatiq and stores the result in emission\_factors.csv; check each factor's region tag, because GLEC road factors are tagged for Europe, not Germany. Climatiq's freight endpoint may be a paid feature, so if it is out of reach, fill the file from published GLEC or UBA values. The distance script sends customers and sites to ORS (coordinates as longitude, latitude) and stores kilometres in distance\_matrix.csv. A pair ORS cannot route, and any customer added in a scenario, gets haversine distance × 1.3 and is flagged as estimated. ORS results need this attribution on the dashboard and in the report: © openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors.

**Validation rules**

- No duplicate IDs; all coordinates fall inside Germany (latitude 47.2 to 55.1, longitude 5.8 to 15.1).
- Total site capacity ≥ 2 × total demand, or the model reports a likely infeasible setup.
- Every customer has at least 2 eligible sites (see section 4), or it is flagged before solving.

## 4. Optimization model

The core is a capacitated facility location model, solved as a mixed-integer program. It chooses sites, a primary and a backup for each customer, and enforces that any single failure can be absorbed.

**Solver.** PuLP with its bundled CBC solver (pulp.PULP\_CBC\_CMD). requirements.txt pins pulp==2.9.0, because newer PuLP releases did not provide PULP\_CBC\_CMD in this project. There is no fallback search: if CBC is not available, the app shows a clear error instead of silently solving another way. Each solve has a time limit and a 1% relative gap, and every result reports its solver status (optimal, time\_limit or infeasible) and gap. The default scenario solves in about 18 s.

**Sets and parameters**

- I = customers, J = candidate sites, with demand d\_i, site capacity Q\_j, fixed cost F\_j and fixed CO₂ f\_j.
- c\_ij = transport CO₂ per tonne from site j to customer i (distance × emission factor), t\_ij = transport cost per tonne on the same leg (distance × cost rate).
- P = carbon price (€/t CO₂). It is used only to report cost including priced CO₂ and as the yardstick for the cost of avoiding CO₂. It does not enter the objective.
- p = probability weight on backup use (share of the year a primary is down), D\_max = maximum backup distance.
- Eligible sites: each customer may only use its 5 nearest candidate sites that lie within D\_max, as primary or as backup. This keeps the model small enough to solve fast.

**Decision variables (binary)**

- y\_j = 1 if site j is opened.
- x\_ij = 1 if j is the primary of customer i.
- z\_ij = 1 if j is the backup of customer i.
- w\_ifj = 1 if customer i has primary f and backup j.

**Objective (the user picks one).** The scenario field objective selects which formula the solver minimizes. The other metric is computed from the same solution and reported. Both formulas count the backup leg with weight p.

CO₂ objective (transport CO₂ plus warehouse CO₂):

```latex
\min \; \sum_{j} f_j\, y_j \;+\; \sum_{i,j} c_{ij}\, d_i \,\big( x_{ij} + p\, z_{ij} \big)
```

Cost objective (warehouse fixed cost plus transport cost):

```latex
\min \; \sum_{j} F_j\, y_j \;+\; \sum_{i,j} t_{ij}\, d_i \,\big( x_{ij} + p\, z_{ij} \big)
```

When several networks tie, the solver adds the other metric with a tiny weight as a tie-break, so the same scenario always returns the same result.

**Constraints**

1. Each customer has exactly one primary and one backup: Σ\_j x\_ij = 1 and Σ\_j z\_ij = 1.
2. Primary and backup differ: x\_ij + z\_ij ≤ 1.
3. Only open sites serve: x\_ij ≤ y\_j and z\_ij ≤ y\_j.
4. Only eligible sites serve: x\_ij = z\_ij = 0 when j is not one of the customer's eligible sites.
5. Normal capacity: Σ\_i d\_i x\_ij ≤ Q\_j y\_j.
6. Exact linking, so w\_ifj = x\_if × z\_ij: w\_ifj ≤ x\_if, w\_ifj ≤ z\_ij, w\_ifj ≥ x\_if + z\_ij − 1, Σ\_j w\_ifj = x\_if and Σ\_f w\_ifj = z\_ij.
7. Failure capacity, for every pair of sites f ≠ j: the normal load of j plus the full demand of customers whose primary is f and whose backup is j must fit in Q\_j. A backup always takes the customer's whole demand.
8. Optional: Σ\_j y\_j = k to force a site count, and forced-open or forced-closed sites from a scenario.

```latex
\sum_i d_i\, x_{ij} \;+\; \sum_i d_i\, w_{ifj} \;\le\; Q_j\, y_j \qquad \text{for every site } j \text{ and every other site } f
```

**Baseline.** A single center-of-gravity warehouse. Weiszfeld's algorithm gives the demand-weighted centroid, shown on the map. The baseline network is the single candidate site that is best for the chosen objective, counting its fixed cost and fixed CO₂, and it is reported with a "not resilient" flag. Capacity is ignored here so the baseline always exists.

**Failure simulation.** For each customer whose primary is the failed site: extra km = km to the backup minus km to the primary; extra CO₂ (t) = e\[i\]\[backup\] minus e\[i\]\[primary\]. The dashboard shows the sum over the moved customers, with distance in km (not tonne-km). Transport only: fixed cost and fixed CO₂ of the sites stay as they are. It also reports customers moved and any capacity overload on the remaining sites.

**k-sweep (only when asked).** Solve for k = 2 to 5 and return cost and CO₂ for each, plus the best k for the chosen objective.

**Cost of avoiding CO₂ (only when asked).** Solve the scenario once for each objective. The KPI is the extra cost of the CO₂-optimal network divided by the CO₂ it saves, in € per tonne, shown next to the carbon price P.

```latex
\text{cost of avoiding CO}_2 \;=\; \frac{C_{\text{co2}} - C_{\text{cost}}}{E_{\text{cost}} - E_{\text{co2}}}
```

C and E are the cost and the CO₂ of each network. Show the number only when both solves are optimal; if either stopped at its time limit, show the solver gap beside it. A run that asks for it takes about two solves, under 60 seconds on the default data.

**Determinism.** The same scenario in gives the same result out, and results are cached by a hash of the scenario.

## 5. Scenario schema

A scenario is a structured object (a Pydantic model) holding only the parameters below. The agent can set nothing else, and every field has a default, so no change returns the baseline run.

| Group | Field | Type | Default | Valid range |
| --- | --- | --- | --- | --- |
| Objective | objective | co2 or cost | co2 | co2 or cost |
| Objective | k\_sweep | boolean | false | true or false |
| Objective | compare\_objectives | boolean | false | true or false |
| Network | close\_sites | list of site IDs | \[\] | Must exist in candidate\_sites |
| Network | force\_open\_sites | list of site IDs | \[\] | Must exist; cannot overlap close\_sites |
| Network | k | integer or null | null (solver chooses) | 2 to number of sites |
| Network | capacity\_scale | float | 1.0 | 0.5 to 2.0 |
| Demand | demand\_scale | float | 1.0 | 0.5 to 3.0 |
| Demand | remove\_customers / add\_customers | list | \[\] | Added customers need lat, lon, demand |
| Green | emission\_mode | diesel or electric | diesel | From emission\_factors.csv |
| Green | emission\_factor\_scale | float | 1.0 | 0.1 to 2.0 |
| Green | warehouse\_co2\_scale | float | 1.0 | 0.5 to 2.0 |
| Green | carbon\_price\_eur\_per\_t | float | 80 | 0 to 500 |
| Cost | transport\_cost\_eur\_per\_tkm | float | 0.10 (placeholder, to confirm) | 0.01 to 1.0 |
| Resilience | max\_backup\_km | float | 500 | 50 to 2000 |
| Resilience | failure\_probability | float | 0.05 | 0 to 0.5 |

k\_sweep and compare\_objectives each add solves, so the agent sets them only when the user asks for them.

**Example translations**

- "What if warehouse B goes down?" → close\_sites = \[B\]
- "Use electric trucks" → emission\_mode = electric
- "Demand grows 30%" → demand\_scale = 1.3
- "Compare 3 vs 4 warehouses with backups within 200 km" → two scenarios, k = 3 and k = 4, max\_backup\_km = 200
- "Minimize cost instead of CO₂" → objective = cost
- "Show me the cost for 2 to 5 warehouses" → k\_sweep = true
- "How much does each tonne of CO₂ saved cost?" → compare\_objectives = true

Out-of-range or unknown values are rejected by validation, and the agent asks the user to clarify instead of guessing.

## 6. LangGraph agent

The agent is a chat. It turns a plain-English request into a validated scenario, solves it, and explains what changed. The solver does all the calculating; the agent only talks. It uses an OpenAI model, with the key and model name read from .env.

**Graph state**

| Field | Holds |
| --- | --- |
| messages | The conversation so far |
| scenario | The current Scenario object (section 5), shared with the objective switch in the sidebar |
| baseline | Result of get\_baseline(objective), reloaded when the objective changes |
| last\_result | Output of the most recent run\_scenario call |
| saved\_scenarios | Saved scenario IDs and results for comparison |
| pending\_question | What the graph is waiting on the user to answer, if anything |

**Nodes**

| Node | Does | Calls |
| --- | --- | --- |
| Parse intent | The LLM fills a Scenario from the message, changing only the fields the user mentioned. Follow-ups build on the current scenario | none (structured output) |
| Validate | Checks ranges, known site IDs and conflicts; goes to Ask the user if invalid | validate\_scenario |
| Run model | Solves the scenario | run\_scenario |
| Feasibility check | Sends an infeasible result to Ask the user with the reason code | none |
| Compare | Builds the change list against the previous run and the baseline | compare\_scenarios, simulate\_failure |
| Explain | The LLM writes a reply of at most 120 words: what the user changed, what happened, one takeaway | none |
| Save | Stores the scenario for side-by-side comparison | none |
| Ask the user | Pauses with one specific question and resumes from the answer | none |

**Conversation behavior**

- The graph uses a checkpointer and a thread ID kept per browser session. Ask the user uses a LangGraph interrupt, so the next message resumes the graph instead of starting a new run.
- If a request is incomplete or invalid, the graph asks one specific question. For an infeasible result it explains the reason in plain English and proposes one fix; if the user says yes, the fix is applied and the model re-runs.
- A reset returns the scenario to the baseline.
- Quick-action buttons for the demo scenarios send their text into the chat, so buttons and typing follow the same path.

**Guardrails**

- The LLM never produces a number; every figure in a reply comes from a tool result. A check rejects any reply that contains a number not found in the tool output; the graph regenerates once, then falls back to a template sentence.
- The Scenario schema is enforced with structured output, so unknown fields are impossible.
- If a request is out of scope (routing, inventory), the agent says so and does not run the model.

## 7. Tools

The agent reaches the solver only through six plain Python functions. Each returns structured JSON, never prose, so every number in an answer comes from the solver.

| Tool | Input | Output |
| --- | --- | --- |
| get\_baseline(objective) | objective (co2 or cost) | Cost, CO₂, open sites and assignments for the best resilient network for that objective, plus the single-warehouse comparison |
| list\_sites() | none | Site IDs, names, locations, capacities, status |
| validate\_scenario(params) | Scenario object | Valid or not, and a list of field-level errors or warnings |
| run\_scenario(params) | Scenario object | Solver status and gap, open sites, primary and backup per customer, cost, CO₂ (transport and warehouse), plus k-sweep results only if k\_sweep is true and the cost of avoiding CO₂ only if compare\_objectives is true |
| simulate\_failure(scenario\_id, site\_id) | Saved scenario, site to remove | Extra km, extra CO₂, customers moved, capacity overloads |
| compare\_scenarios(ids) | List of saved scenario IDs | Side-by-side deltas in cost, CO₂, site count and resilience, plus a change list (below) |

**Change list.** Every run is compared with the previous run and with the baseline. The result lists the parameters changed (old value and new value), the change in each KPI (absolute and percent), the sites opened and closed, and the customers whose primary or backup moved. If nothing differs, it says so.

**Infeasibility contract.** When no solution exists, run\_scenario returns status = infeasible with a reason code (backup\_distance, capacity, too\_few\_sites) and the customers or sites involved, so the agent can propose a specific relaxation.

**Time-limited results.** If a solve stops at its time limit, the result says so and includes the gap, and the agent tells the user.

## 8. Dashboard

A Streamlit app in a green and white theme. The left column is the chat with its history and quick-action buttons, plus the objective switch (CO₂ or cost), which shares one scenario state with the chat. The right column shows the What changed card, a row of KPI cards, and four views, all driven by the current scenario.

&#91;embedded content: dashboard layout · chat, What changed card, KPI cards, 4 views\]

The sketch shows the layout: chat and quick actions on the left, the What changed card above the KPI cards on the right, and the four views below.

| View | Shows |
| --- | --- |
| Network map | Open sites, customers, primary links (solid) and backup links (dashed), plus the center-of-gravity point |
| k curve | Total cost and CO₂ for k = 2 to 5, with the recommended k marked; shown only after a k-sweep |
| Failure view | Pick a site, see extra km, extra CO₂, customers moved and the capacity load on the remaining sites |
| Scenario table | Saved scenarios side by side, each shown as a delta against the baseline |

**What changed card.** After every run, a card above the KPI row lists the parameters changed (old value to new value), each KPI's change against the previous run and against the baseline (absolute and percent), the sites opened and closed, and how many customers got a new primary or backup, with an expandable table. If nothing differs it says No change. Each result also shows its solver status and gap.

**KPIs reported for every run**

- Total CO₂ (t/year) and its split: transport vs warehouse operation.
- Total annual cost (€), also shown with CO₂ priced at P.
- Number of open warehouses, and average distance to primary and to backup.
- Price of resilience: cost and CO₂ of the resilient network minus the single center-of-gravity baseline.
- Worst-case failure: the site whose loss adds the most CO₂, and its capacity utilization.
- Cost of avoiding CO₂ (only when compare\_objectives is true): extra € per tonne, shown against P.

**Report.** A Download report button, or a request in the chat (send me a report), builds one HTML report from the current run: the user's request, the What changed card, the KPI cards, the k curve if it was run, the network map as an image, the delta against the baseline, the assumptions, and the ORS attribution. All numbers come from the solver output. There is no email sending.

**Look and feel.** The palette goes into .streamlit/config.toml and into the Plotly and Folium styling. The footer carries the ORS attribution.

| Role | Color | Use |
| --- | --- | --- |
| Forest | #1B4332 | Headings and text |
| Green | #2D6A4F | Buttons, primary links, CO₂ series |
| Leaf | #52B788 | Open sites and accents |
| Mint | #D8F3DC | Cards and secondary background |
| White | #FFFFFF | Page background |
| Sky | #277DA1 | Cost series |
| Amber | #E9C46A | Capacity close to its limit |
| Clay | #C8553D | Failed or overloaded sites |

**Demo scenarios to ship with the project**

1. Baseline: best resilient network for the default data.
2. Largest site fails: close it and show the rerouted load.
3. Electric fleet: emission factor cut by 60%, does the best k change?
4. Demand +30%: is capacity still enough with failure cover?
5. Tight backup limit (150 km): find where it becomes infeasible and what fixes it.
6. Objective switch: solve for CO₂ and for cost, show which sites change and the cost of avoiding CO₂.

## 9. Stack and folder structure

| Layer | Tools | Role |
| --- | --- | --- |
| Data | pandas | Load and validate customers, sites, factors |
| Distances | OpenRouteService Matrix API (driving-hgv), fetched once | Customer × site distance matrix, cached; haversine only for pairs ORS cannot route and for added customers |
| Emissions | Climatiq API, fetched once | CO₂ per tonne-km by vehicle type, cached in a CSV |
| Optimization | PuLP 2.9.0 with CBC, NumPy | The MIP, center of gravity |
| Agent | LangGraph, LangChain, an OpenAI model, Pydantic | Scenario parsing, validation, routing, explanation |
| Tool interface | Plain Python functions | The six tools in section 7 |
| Interface | Streamlit with a green theme, Plotly, Folium | Chat, objective switch, KPIs, maps, curves, tables |
| Report | Python HTML template | The downloadable report |

**APIs used.** OpenRouteService and Climatiq (each fetched once), and OpenAI for the agent. Keys are read from .env.

**Folder structure**

```text
green-warehouse/
├── data/          customers.csv, candidate_sites.csv, emission_factors.csv, distance_matrix.csv (cached)
├── src/           distances, emissions, baseline, model, simulate, tools, diff, report
├── agent/         schema.py, graph.py, prompts.py
├── tests/         solver, failure, diff and agent tests
├── .streamlit/    config.toml (green theme)
├── app.py         Streamlit dashboard
├── requirements.txt, .env.example, README.md
```

## 10. Build plan, acceptance criteria and risks

Four phases; each ends with a check that must pass before the next begins. The project is complete after phase 4. Nothing outside this spec gets added.

| Phase | Deliverable | Done when |
| --- | --- | --- |
| 1. Data and distances | Clean customers, sites, factors and the cached ORS distance matrix | Validation rules in section 3 pass |
| 2. Optimization core | run\_scenario with primary, backup and failure capacity, plus baseline, failure simulation, and the optional k-sweep and compare\_objectives, each with a CO₂ or cost objective | A hand-checked 5-customer, 3-site case matches an independent brute-force check under both objectives, and so does a second case where the objectives choose different sites |
| 3. Scenario agent and chat | Pydantic schema, LangGraph agent, chat, change list | All 6 demo scenarios run end to end from plain English |
| 4. Dashboard and report | Streamlit dashboard in the green theme, scenario table, downloadable report | A user completes a scenario and downloads the report without touching code |

**Acceptance criteria**

- Every customer has a distinct primary and backup in every feasible result.
- Closing any single open site never overloads a remaining site.
- No number in an agent reply differs from the solver output.
- Infeasible scenarios return a plain-English reason and one suggested fix; saying yes applies it and re-runs. Never an empty answer.
- Re-running the same scenario returns identical results.
- Switching the objective optimizes the chosen metric: the CO₂ objective never gives higher CO₂ than the cost objective, and the cost objective never gives higher cost than the CO₂ objective.
- On the default data, the CO₂ objective and the cost objective open different sets of sites, and the failure-capacity constraint is binding (tripling capacity changes cost).
- A follow-up such as "now force 4 warehouses" keeps all earlier settings and changes only k, and the What changed card lists exactly the parameters that differ.
- An invalid request (for example an unknown site W99) gets a clarifying question, never a guess or a crash.
- Failure simulation reports extra distance in km and extra CO₂ in t, as defined in section 4.
- Every result shows its solver status and gap. If CBC is not available, the app shows a clear error and does not solve another way.
- The default scenario solves in under 60 seconds; a run with compare\_objectives also stays under 60 seconds.

**Risks**

| Risk | Mitigation |
| --- | --- |
| Failure-capacity constraints make the model slow | Eligible sites limited to the 5 nearest within D\_max, exact w linking, a time limit and 1% gap, extra solves only on request |
| The cost of avoiding CO₂ is unreliable when a solve stops at its time limit | Show it only when both solves are optimal, otherwise show the gap beside it |
| The agent invents or misreads parameters | Strict schema, validation node, the solver is the only source of numbers |
| Illustrative site data looks arbitrary | Replace fixed cost and fixed CO₂ with real figures before reading results as findings |
| ORS free limits (500 matrix requests a day) or an outage | Fetch once, cache to disk, flag any haversine cell |
| Climatiq freight endpoint is not on the free plan | Keep emission\_factors.csv with published GLEC or UBA values |
| Transport cost rate is a placeholder | Replace it with a sourced € per tonne-km rate before reading cost results |
| Scope creep | Out of scope is fixed in section 2; only bug fixes and the criteria above remain |

## 11. Setup and run

Secrets are read from a .env file in the project root and are never committed. It holds the OpenAI key and model name for the agent, plus the OpenRouteService and Climatiq keys. A .env.example lists the same variable names without values.

```bash
pip install -r requirements.txt
streamlit run app.py
pytest
```

requirements.txt pins pulp==2.9.0. The tests cover the hand-checked 5-customer, 3-site case, the second case where the objectives differ, the failure simulation, the change list, and the acceptance criteria in section 10.
