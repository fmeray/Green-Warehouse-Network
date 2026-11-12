# Stack decision

## Primary stack: LangGraph

This MVP uses LangGraph as its primary application stack. The product is a conversational warehouse-network planner: a user confirms an optimization objective, describes a scenario in natural language, receives clarification when needed, and can continue changing the same scenario across multiple turns. LangGraph fits this workflow because it provides:

- explicit state for the current scenario, previous result, saved scenarios, and chat messages;
- conditional routing for validation errors, infeasible scenarios, questions, out-of-scope requests, and successful runs;
- checkpointed conversation state through a thread ID and `MemorySaver`;
- interrupt/resume behavior for clarification and suggested fixes; and
- a clean boundary between the LLM's intent/tool orchestration and the deterministic PuLP/CBC optimization model.

The MVP therefore keeps the LLM responsible for language understanding, explanation, and report writing, while `src/tools.py` and `src/model.py` remain the source of truth for validation, feasibility, assignments, costs, CO₂, and failure-cover constraints. This separation is important for a planning use case where a plausible-sounding answer is not sufficient: every plan must come from the solver and expose its status and warnings.

## Why n8n is secondary for this MVP

n8n is useful for integration and operational automation, but it is not the primary runtime for this version. The MVP needs stateful, branching, multi-turn decision support and a Python-native mixed-integer optimization model. Building that core loop in n8n would add workflow plumbing around the main product logic and make solver state, clarification turns, and deterministic testing less natural.

n8n is the secondary stack for future GTM and production-integration work: scheduled scenario runs, Slack or email alerts, exports to spreadsheets or BI tools, CRM hand-offs, and connections to WMS/ERP systems. Those are valuable after the planning experience is validated, but they are downstream automation rather than the core warehouse-allocation decision loop.

## MVP boundary

The current implementation is a Streamlit interface backed by a LangGraph agent. LangChain's structured output and tool-calling interfaces connect the agent to the six structured functions in `src/tools.py`; PuLP 2.9.0 with CBC performs the optimization; local CSV and cached distance data provide the inputs. This gives the MVP a small, testable architecture while leaving n8n available as an integration layer in later sprints.
