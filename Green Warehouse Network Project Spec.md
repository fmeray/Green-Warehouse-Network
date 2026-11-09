# Green Warehouse Network: Project Spec

Nov 9, 2026 · @fatma

## 1. Problem and objectives

**Decide how many warehouses to open, where to put them, and which two serve each customer (a primary and a backup), so that total CO₂ emissions and cost are minimized and no single warehouse failure leaves a customer unserved.**

The project answers five questions:

1. How many warehouses are needed (minimum 2, because every customer needs a backup)?
2. Where should they be located?
3. Which warehouse is each customer's primary, and which is the backup?
4. What does resilience cost in CO₂ and money compared with one center-of-gravity warehouse?
5. How do the answers change when the user alters inputs (a site closes, demand grows, trucks go electric)?

**Success criteria**

- Every customer has a primary and a backup, and they are different open sites.
- If any one warehouse fails, the others can absorb its demand within capacity.
- The tool reports the cost and CO₂ curve for k = 2 to 5 and recommends one k.
- A user can describe a scenario in plain English and get a solved result compared with the baseline in under 60 seconds.

## 2. Scope and assumptions

The project is a static, single-period facility location study with a conversational scenario layer. It does not use live operational data.

| In scope | Out of scope |
| --- | --- |
| Warehouse count, location, and customer assignment | Vehicle routing and last-mile scheduling |
| Primary plus backup assignment | Inventory and stock-level optimization |
| CO₂ and cost from transport plus warehouse operation | Multi-period or dynamic network design |
| Single-site failure resilience | Simultaneous multi-site failures |
| Natural-language what-if scenarios | Real-time data or live tracking |

**Assumptions (change any of these in section 5)**

- Customer locations and demand are static, from a public dataset or generated.
- Candidate warehouse sites are fixed in advance (a grid or a chosen city list).
- Transport emissions = distance × load × emission factor (g CO₂ per tonne-km).
- Each open warehouse has a fixed cost and a fixed CO₂ footprint per year.
- "Backup" means a second open warehouse within a maximum distance that can absorb the primary's demand if it fails.
- Single center of gravity is a comparison baseline only, since it cannot meet the backup rule.

## 3. Data spec

Four static inputs feed the model. Sizes below are the target for a first version: 30 to 60 customers and 8 to 15 candidate sites.

| Dataset | Fields | Source | Notes |
| --- | --- | --- | --- |
| customers.csv | id, name, lat, lon, demand (tonnes/year) | Public dataset (e.g. Olist or a city list) or generated | Demand must be positive |
| candidate\_sites.csv | id, name, lat, lon, capacity (tonnes/year), fixed\_cost (€/year), fixed\_co2 (t CO₂/year) | Chosen city list or a grid over the customer area | Capacity should total at least 2× total demand so failures stay feasible |
| emission\_factors.csv | mode (diesel, electric), g CO₂ per tonne-km | DEFRA or GLEC published factors | Used as defaults; the user can override |
| distance matrix | customer × site distance in km | Computed: haversine first, OpenRouteService later | Cached to disk so the solver never calls an API |

**Derived matrix**

Emissions for customer i served from site j: e\[i\]\[j\] = distance\[i\]\[j\] × demand\[i\] × factor ÷ 1,000,000 (t CO₂).

**Validation rules**

- No duplicate IDs; all coordinates within valid ranges.
- Total site capacity ≥ 2 × total demand, or the model reports it as a likely infeasible setup.
- Every customer has at least 2 candidate sites within the maximum backup distance, or it is flagged before solving.

## 4. Optimization model

The core is a capacitated facility location model, solved as a mixed-integer program with PuLP or OR-Tools. It chooses sites, a primary and a backup for each customer, and enforces that any single failure can be absorbed.

**Sets and parameters**

- I = customers, J = candidate sites, with demand d\_i, site capacity Q\_j, fixed cost F\_j and fixed CO₂ f\_j.
- c\_ij = transport CO₂ per tonne from site j to customer i (distance × emission factor), and t\_ij = transport cost per tonne on the same leg.
- P = carbon price (€/t CO₂), so cost and CO₂ combine into one objective; both are also reported separately.
- p = probability weight on backup use (share of the year a primary is down), D\_max = maximum backup distance.

**Decision variables (binary)**

- y\_j = 1 if site j is opened.
- x\_ij = 1 if j is the primary of customer i.
- z\_ij = 1 if j is the backup of customer i.
- w\_ifj = 1 if customer i has primary f and backup j (linking variable for the failure constraint).

**Objective**

```latex
\min \; \sum_{j} (F_j + P f_j)\, y_j \;+\; \sum_{i,j} (t_{ij} + P c_{ij})\, d_i \,\big( x_{ij} + p\, z_{ij} \big)
```

**Constraints**

1. Each customer has exactly one primary and one backup: Σ\_j x\_ij = 1 and Σ\_j z\_ij = 1.
2. Primary and backup differ: x\_ij + z\_ij ≤ 1.
3. Only open sites serve: x\_ij ≤ y\_j and z\_ij ≤ y\_j.
4. Backup distance limit: z\_ij = 0 whenever dist\_ij > D\_max.
5. Normal capacity: Σ\_i d\_i x\_ij ≤ Q\_j y\_j.
6. Failure capacity, for every pair of sites f ≠ j: normal load of j plus the demand redirected from f to j must fit in Q\_j.
7. Linking: w\_ifj ≥ x\_if + z\_ij − 1.
8. Optional: Σ\_j y\_j = k to force a site count, and forced-open or forced-closed sites from a scenario.

**Baseline and checks**

- Baseline: single center of gravity (demand-weighted centroid, found with Weiszfeld's algorithm), reported as cost and CO₂ with a "not resilient" flag.
- Heuristic: weighted k-means to seed candidate sites and warm-start the solver.
- Failure simulation: for each open site, remove it, reassign its customers to their backups, and report extra distance, extra CO₂, and any capacity overload.
- Sweep: solve for k = 2 to 5 and return the cost and CO₂ for each, plus the cheapest k.

## 5. Scenario schema

A scenario is a structured object (a Pydantic model) holding only the parameters below. The agent can set nothing else, and every field has a default, so "no change" returns the baseline run.

| Group | Field | Type | Default | Valid range |
| --- | --- | --- | --- | --- |
| Network | close\_sites | list of site IDs | \[\] | Must exist in candidate\_sites |
| Network | force\_open\_sites | list of site IDs | \[\] | Must exist; cannot overlap close\_sites |
| Network | k | integer or null | null (solver chooses) | 2 to number of sites |
| Network | capacity\_scale | float | 1.0 | 0.5 to 2.0 |
| Demand | demand\_scale | float | 1.0 | 0.5 to 3.0 |
| Demand | remove\_customers / add\_customers | list | \[\] | Added customers need lat, lon, demand |
| Green | emission\_mode | diesel or electric | diesel | From emission\_factors.json |
| Green | emission\_factor\_scale | float | 1.0 | 0.1 to 2.0 |
| Green | warehouse\_co2\_scale | float | 1.0 | 0.5 to 2.0 |
| Green | carbon\_price\_eur\_per\_t | float | 80 | 0 to 500 |
| Resilience | max\_backup\_km | float | 300 | 50 to 2000 |
| Resilience | failure\_probability | float | 0.05 | 0 to 0.5 |
| Resilience | backup\_must\_cover\_full\_demand | boolean | true | true or false |

**Example translations**

- "What if warehouse B goes down?" → close\_sites = \[B\]
- "Use electric trucks" → emission\_mode = electric
- "Demand grows 30%" → demand\_scale = 1.3
- "Compare 3 vs 4 warehouses with backups within 200 km" → two scenarios, k = 3 and k = 4, max\_backup\_km = 200

Out-of-range or unknown values are rejected by validation, and the agent asks the user to clarify instead of guessing.

## 7. Tool interface

The agent reaches the solver only through six functions. Each returns structured JSON, never prose, so every number in an answer comes from the solver. They are plain Python functions first and can be wrapped as MCP tools later without changing their signatures.

| Tool | Input | Output |
| --- | --- | --- |
| get\_baseline() | none | Cost, CO₂, open sites and assignments for the best resilient network, plus the single-warehouse comparison |
| list\_sites() | none | Site IDs, names, locations, capacities, status |
| validate\_scenario(params) | Scenario object | Valid or not, and a list of field-level errors or warnings |
| run\_scenario(params) | Scenario object | Status (optimal, infeasible), open sites, primary and backup per customer, cost, CO₂, k-sweep results |
| simulate\_failure(scenario\_id, site\_id) | Saved scenario, site to remove | Extra km, extra CO₂, customers moved, capacity overloads |
| compare\_scenarios(ids) | List of saved scenario IDs | Side-by-side deltas in cost, CO₂, site count, resilience |

**Infeasibility contract.** When no solution exists, run\_scenario returns status = infeasible with a reason code (backup\_distance, capacity, too\_few\_sites) and the customers or sites involved, so the agent can propose a specific relaxation.

**Determinism.** Same scenario in, same result out: the solver uses a fixed seed and results are cached by a hash of the scenario.

## 8. Architecture and tool stack

&#91;embedded content: architecture · 5 layers plus optional n8n\]

The dashboard talks to the agent, the agent calls six tools, and only the optimization core produces results. n8n is an optional trigger and delivery layer around the agent.

| Layer | Tools | Role |
| --- | --- | --- |
| Data | pandas, GeoPandas | Load and validate customers, sites, factors |
| Distances | haversine, OpenRouteService API | Customer × site distance matrix, cached |
| Emissions | DEFRA or GLEC factors, Climatiq API | CO₂ per tonne-km by vehicle type |
| Optimization | PuLP or OR-Tools, scikit-learn, SciPy | MIP, k-means seeding, center of gravity |
| Agent | LangGraph, LangChain, an LLM, Pydantic | Scenario parsing, validation, routing, explanation |
| Tool interface | Python functions, optional MCP server | The six tools in section 7 |
| Interface | Streamlit, Plotly or Folium | Chat, maps, curves, tables |
| Automation | n8n (optional) | Webhook trigger, scheduled runs, Slack or email delivery |

**APIs used (three):** Nominatim for geocoding, OpenRouteService for road distances, Climatiq for emission factors. **ML:** not used in the core, because nothing needs predicting; demand forecasting is a possible extension.

**Folder structure**

```text
green-warehouse/
├── data/          customers, sites, factors, cached matrices
├── src/           distances, emissions, baseline, model, simulate, tools
├── agent/         schema.py, graph.py, prompts.py
├── app.py         Streamlit dashboard
└── README.md
```

## 9. Outputs and KPIs

The Streamlit dashboard has a chat panel on the left and four views on the right, all driven by the current scenario.

| View | Shows |
| --- | --- |
| Network map | Open sites, customers, primary links (solid) and backup links (dashed) |
| Trade-off curve | Total cost and CO₂ for k = 2 to 5, with the recommended k marked |
| Failure view | Pick a site, see extra km, extra CO₂, and capacity load on the remaining sites |
| Scenario table | Saved scenarios side by side, each shown as a delta against the baseline |

**KPIs reported for every run**

- Total CO₂ (t/year) and its split: transport vs warehouse operation.
- Total annual cost (€) including priced CO₂.
- Number of open warehouses and average distance to primary and to backup.
- Price of resilience: cost and CO₂ of the resilient network minus the single center-of-gravity baseline.
- Worst-case failure: the site whose loss adds the most CO₂ and its capacity utilization.

**Demo scenarios to ship with the project**

1. Baseline: best resilient network for the default data.
2. Largest site fails: close it and show the rerouted load.
3. Electric fleet: emission factor cut by 60%, does the best k change?
4. Demand +30%: is capacity still enough with failure cover?
5. Tight backup limit (150 km): find where it becomes infeasible and what fixes it.

## 10. Build plan, acceptance criteria and risks

The build runs in five phases, and each phase ends with a check that must pass before the next begins. The project is complete and demoable after phase 3; phases 4 and 5 add the interface and automation.

| Phase | Deliverable | Done when |
| --- | --- | --- |
| 1. Data and distances | Clean customers, sites, factors, cached distance matrix | Validation rules in section 3 pass |
| 2. Optimization core | run\_scenario with primary, backup, failure capacity, plus baseline and k-sweep | A hand-checked 5-customer, 3-site case matches the solver |
| 3. Scenario agent | Pydantic schema and LangGraph agent calling the tools | All 5 demo scenarios run end to end from plain English |
| 4. Dashboard | Streamlit chat, map, curve, failure view, scenario table | A user completes a scenario without touching code |
| 5. Optional extras | MCP wrapper for the tools, n8n webhook and scheduled summary | Webhook returns a scenario summary to Slack or email |

**Acceptance criteria**

- Every customer has a distinct primary and backup in every feasible result.
- Closing any single open site never overloads a remaining site.
- No number in an agent reply differs from the solver output.
- Infeasible scenarios return a reason and a suggested relaxation, never an empty answer.
- Re-running the same scenario returns identical results.

**Risks**

| Risk | Mitigation |
| --- | --- |
| Failure-capacity constraints make the model slow | Prune variables beyond D\_max, warm-start with k-means, cap site and customer counts |
| The agent invents or misreads parameters | Strict schema, validation node, solver is the only source of numbers |
| Synthetic data looks arbitrary | Use real city coordinates and a published emission-factor source |
| Scope creep into routing or inventory | Held out of scope in section 2 |
