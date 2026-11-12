# Green Warehouse Network

Green Warehouse Network is a deterministic Germany-wide warehouse-network planner. Given 24 customers, 12 candidate warehouses, demand, capacity, distance, cost, and emissions inputs, it selects a resilient network with a primary and backup warehouse for every customer. Users can optimize for CO₂ or cost, test warehouse closures and operating assumptions, inspect k-curves, compare scenarios, and download an HTML report.

The MVP combines a Streamlit dashboard, a LangGraph conversational agent, and a PuLP/CBC optimization model. See [stack_decision.md](stack_decision.md) for the rationale and [gtm_future_sprints.md](gtm_future_sprints.md) for the next GTM experiments.

## Setup

Python 3.10+ is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

The dashboard's chat agent and report writer require an OpenAI API key. Create a local `.env` file (it is git-ignored):

```dotenv
OPENAI_API_KEY=your-key
OPENAI_MODEL=gpt-4o-mini
```

The distance cache is already prepared in `data/cache/`, so the normal run does not call a routing API. If the cache is unavailable, the application uses the documented haversine × 1.3 estimate and reports the distance provenance in the dashboard and report.

## Run

Start the dashboard from the repository root:

```bash
streamlit run app.py
```

In the dashboard:

1. Choose CO₂ or cost and click **Confirm objective**.
2. The agent runs the default scenario and displays the network on the map.
3. Describe changes in the chat, such as closing a warehouse, increasing demand, switching to electric trucks, or requesting a k-sweep.
4. Use **Generate report with LLM** to create a session report and download it as HTML.

The objective must be confirmed before a solver run. Chat requests are handled in the current Streamlit session and retain their scenario through one LangGraph thread. If the model asks a clarification question or proposes a fix for an infeasible scenario, answer in the chat to resume the graph.

Run the deterministic tool directly:

```bash
python3 -c "from src.tools import run_scenario; print(run_scenario({})['status'])"
```

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

## Architecture and workflow

```text
Streamlit UI (app.py)
        |
        v
LangGraph stateful agent (agent/graph.py)
  parse intent -> validate -> ask/interrupt when needed
        |
        v
LangChain structured output + required tool call
        |
        v
Structured tools (src/tools.py)
  validate / baseline / solve / failure / compare
        |
        v
PuLP 2.9.0 + CBC (src/model.py)
        |
        v
Assignments, capacity, cost, CO₂, solver status, warnings
        |
        v
Map, KPI cards, k-curve, scenario table, LLM report
```

The normal request flow is:

- `app.py` stores the selected objective, chat history, session events, and LangGraph thread ID.
- `agent/graph.py` extracts only explicitly requested scenario fields into a Pydantic intent schema, merges them with the current scenario, and routes questions or invalid inputs to clarification.
- `src/tools.py` validates the scenario, materializes customer/site inputs, calls the solver, caches completed scenarios, and returns structured success or error data.
- `src/model.py` solves the resilient facility-location problem with primary and backup coverage, capacity constraints, backup-distance limits, and either CO₂ or cost as the objective.
- The agent compares the new result with the previous result, explains assignment and metric changes, and saves completed scenarios for the dashboard.
- The UI renders the map and metrics. An optional LLM report is generated from the recorded session events; it does not replace solver output.

## File map

| Path | Responsibility |
| --- | --- |
| `app.py` | Streamlit dashboard, session state, chat controls, maps, metrics, k-curve, scenarios, and report download |
| `agent/graph.py` | LangGraph state machine, OpenAI intent parsing, tool orchestration, clarification, comparison, and report generation |
| `agent/schema.py` | LLM-facing Pydantic schemas for partial scenario requests |
| `src/tools.py` | Six structured functions and scenario cache: `get_baseline`, `list_sites`, `validate_scenario`, `run_scenario`, `simulate_failure`, and `compare_scenarios` |
| `src/model.py` | PuLP/CBC mixed-integer optimization model and solver status handling |
| `src/schema.py` | Validated scenario fields, defaults, and ranges |
| `src/data.py` | CSV loading and input-data validation |
| `src/geo.py` | Cached routing distances, fallback distance calculation, and provenance |
| `src/map.py` | Folium network/customer map rendering |
| `src/climatiq.py` | Emissions-factor integration helpers |
| `data/customers.csv` | 24 Germany-wide customer inputs and annual demand |
| `data/candidate_sites.csv` | 12 candidate warehouses with capacity, fixed cost, and fixed CO₂ |
| `data/emission_factors.csv` | Diesel/electric emissions factors |
| `data/cache/` | Prepared distance matrix cache used at runtime when available |
| `tests/` | Solver-path, agent-error, and intent-paraphrase tests |
| `stack_decision.md` | Primary LangGraph versus secondary n8n decision |
| `gtm_future_sprints.md` | Three future GTM sprints after the MVP |

## Scenario API

The six structured functions in `src/tools.py` are the stable tool boundary:

- `get_baseline(objective)` provides center-of-gravity and single-warehouse references.
- `list_sites()` returns candidate warehouse metadata.
- `validate_scenario(params)` checks ranges, sites, customers, capacity warnings, and reachability.
- `run_scenario(params)` solves the resilient network and can run `k_sweep` and `compare_objectives`.
- `simulate_failure(scenario_id, site_id)` evaluates a saved scenario after a site failure.
- `compare_scenarios(ids)` compares saved scenario results.

`run_scenario({})` runs the default CO₂ scenario. The defaults include `max_backup_km=500`, `k_sweep=false`, and `compare_objectives=false`. All solves use CBC; the response exposes solver status and relative gap. The app does not silently switch to another solver or use an LLM fallback for optimization.

Distance attribution shown by the app and report: © openrouteservice.org by HeiGIT | Map data © OpenStreetMap contributors.
