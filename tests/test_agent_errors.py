import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent.graph import (
    OpenAIRequestError,
    _change_list,
    _explain,
    _merge_scenario,
    _normalize_markdown_tables,
    _structured_intent,
    ask,
    build_graph,
)
from src.schema import scenario_from_dict


BASELINE = {"network": {"objective": "co2", "total_cost_eur": 900, "total_co2_t": 90}}


def solved_result(scenario):
    return {
        "ok": True,
        "status": "optimal",
        "solver_status": "optimal",
        "solver_relative_gap": 0.01,
        "objective": scenario["objective"],
        "scenario_id": "scenario-1",
        "scenario": scenario,
        "total_cost_eur": 1000,
        "total_co2_t": 100,
        "k": 3,
        "open_sites": ["W01", "W02", "W03"],
        "assignments": {},
    }


class FakeStructured:
    def __init__(self, responses):
        self.responses = responses

    def invoke(self, messages):
        if callable(self.responses):
            return self.responses(messages)
        return self.responses.pop(0)


class FakeModel:
    def __init__(self, structured, replies=None):
        self.structured = structured
        self.replies = list(replies or ["The scenario is complete."])
        self.structured_calls = 0
        self.invoke_calls = 0

    def with_structured_output(self, schema):
        self.structured_calls += 1
        return FakeStructured(self.structured)

    def invoke(self, messages):
        self.invoke_calls += 1
        return AIMessage(content=self.replies.pop(0) if self.replies else "The scenario is complete.")

    def bind_tools(self, tools, **kwargs):
        model = self

        class BoundToolModel:
            def invoke(self, messages):
                prompt = messages[-1].content
                scenario = json.loads(prompt.split("Scenario JSON:\n", 1)[1])
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "id": "fake-run-scenario",
                        "name": "run_scenario",
                        "args": {"params": scenario},
                    }],
                )

        return BoundToolModel()


def valid_scenario(_scenario):
    return {"valid": True, "warnings": [], "tool_warnings": []}


class AgentGraphTests(unittest.TestCase):
    def test_openai_parse_failure_is_returned_as_a_clear_chat_error(self):
        class BrokenModel:
            def with_structured_output(self, _schema):
                class BrokenStructured:
                    def invoke(self, _messages):
                        raise RuntimeError("connection refused")

                return BrokenStructured()

        result = ask("use electric trucks", chat_model=BrokenModel())
        self.assertEqual(result["error_type"], "openai_failure")
        self.assertIn("OpenAI intent parsing failed: connection refused", result["message"])

    def test_structured_parser_does_not_have_a_keyword_fallback(self):
        class EmptyModel:
            def with_structured_output(self, _schema):
                class EmptyStructured:
                    def invoke(self, _messages):
                        return {"intent": "scenario", "scenario": {}}

                return EmptyStructured()

        envelope = _structured_intent(
            EmptyModel(), "what happens if we change trucks to electric?", scenario_from_dict({}).model_dump()
        )
        self.assertEqual(envelope.scenario.model_dump(exclude_none=True), {})

    def test_follow_up_closures_are_cumulative(self):
        merged = _merge_scenario(
            {"objective": "co2", "close_sites": ["W01"]},
            {"close_sites": ["W02"]},
        )
        self.assertEqual(merged["close_sites"], ["W01", "W02"])

    def test_reopening_a_closed_site_removes_the_close_constraint(self):
        merged = _merge_scenario(
            {"objective": "co2", "close_sites": ["W01"]},
            {"force_open_sites": ["W01"]},
        )
        self.assertEqual(merged["close_sites"], [])
        self.assertEqual(merged["force_open_sites"], ["W01"])

    def test_open_city_is_not_silently_dropped(self):
        fake = FakeModel([{"intent": "scenario", "scenario": {"force_open_sites": ["W01"]}}])

        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            result = graph.invoke(
                {"messages": [HumanMessage(content="What if we open Hamburg site?")]},
                {"configurable": {"thread_id": "open-city"}},
            )

        self.assertEqual(run.call_args.args[0]["force_open_sites"], ["W01"])
        self.assertNotIn("W01", run.call_args.args[0]["close_sites"])
        self.assertNotIn("__interrupt__", result)

    def test_open_request_overrides_llm_close_field(self):
        fake = FakeModel([
            {"intent": "scenario", "scenario": {"close_sites": ["W01"]}},
            {"intent": "scenario", "scenario": {"force_open_sites": ["W01"]}},
        ])
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "open-overrides-close"}}
            graph.invoke({"messages": [HumanMessage(content="Close Hamburg site")]}, config)
            graph.invoke({"messages": [HumanMessage(content="Open Hamburg site")]}, config)

        second_scenario = run.call_args.args[0]
        self.assertEqual(second_scenario["close_sites"], [])
        self.assertEqual(second_scenario["force_open_sites"], ["W01"])

    def test_closing_already_closed_site_does_not_resolve_a_new_network(self):
        fake = FakeModel([
            {"intent": "scenario", "scenario": {"close_sites": ["W01"]}},
            {"intent": "scenario", "scenario": {"close_sites": ["W02"]}},
        ])

        def solved(scenario):
            return {
                **solved_result(scenario),
                "scenario": scenario,
                "open_sites": ["W03", "W04"],
                "assignments": {},
            }

        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved
        ) as run:
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "no-op-closure"}}
            graph.invoke({"messages": [HumanMessage(content="Close Hamburg")]}, config)
            second = graph.invoke(
                {"messages": [HumanMessage(content="Close Berlin")]},
                config,
            )
        self.assertEqual(run.call_count, 1)
        self.assertTrue(second["last_result"]["no_op"])
        self.assertEqual(second["last_result"]["open_sites"], ["W03", "W04"])

    def test_closing_site_that_was_not_open_is_reported_as_noop(self):
        changes = _change_list({
            "previous_result": {
                "scenario": {"objective": "co2", "close_sites": ["W01"]},
                "open_sites": ["W03", "W04"],
                "assignments": {},
            },
            "last_result": {
                "scenario": {"objective": "co2", "close_sites": ["W01", "W02"]},
                "open_sites": ["W03", "W04"],
                "assignments": {},
            },
            "scenario": {"objective": "co2", "close_sites": ["W01", "W02"]},
        })
        berlin = next(item for item in changes["requested_site_status"] if item["site_id"] == "W02")
        self.assertEqual((berlin["before"], berlin["after"]), ("not open", "not open"))
        self.assertEqual(changes["sites_closed"], [])
        self.assertEqual(changes["sites_opened"], [])

    def test_question_about_previous_assignments_is_answered(self):
        fake = FakeModel([
            {"intent": "scenario", "scenario": {"close_sites": ["W01"]}},
            {"intent": "question", "scenario": {}, "question": "Why did assignments change?"},
        ])
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "assignment-question"}}
            graph.invoke({"messages": [HumanMessage(content="Close Hamburg")]}, config)
            answer = graph.invoke(
                {"messages": [HumanMessage(content="Why did you change the network routes?")]},
                config,
            )
        self.assertNotIn("__interrupt__", answer)
        self.assertEqual(run.call_count, 1)

    def test_markdown_table_rows_are_not_split_by_blank_lines(self):
        answer = "| A | B |\n\n|---|---|\n\n| 1 | 2 |"
        self.assertEqual(
            _normalize_markdown_tables(answer),
            "| A | B |\n|---|---|\n| 1 | 2 |",
        )

    def test_objective_confirmation_runs_default_without_inventing_changes(self):
        fake = FakeModel([{"intent": "scenario", "scenario": {"close_sites": ["W01"]}}])
        default = scenario_from_dict({"objective": "co2"}).model_dump()
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            state = graph.invoke(
                {
                    "messages": [HumanMessage(content="Run the default scenario.")],
                    "scenario": default,
                    "execute_scenario": True,
                },
                {"configurable": {"thread_id": "objective-confirmation"}},
            )
        self.assertEqual(fake.structured_calls, 0)
        self.assertEqual(run.call_args.args[0]["close_sites"], [])
        self.assertEqual(state["scenario"]["close_sites"], [])

    def test_follow_up_keeps_earlier_settings(self):
        fake = FakeModel([
            {"intent": "scenario", "scenario": {"emission_mode": "electric"}},
            {"intent": "scenario", "scenario": {"demand_scale": 1.3}},
        ])
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.get_baseline", return_value=BASELINE
        ), patch("agent.graph.run_scenario", side_effect=solved_result):
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "follow-up"}}
            graph.invoke({"messages": [HumanMessage(content="Use electric trucks")]}, config)
            state = graph.invoke({"messages": [HumanMessage(content="Demand grows 30%.")]}, config)
        self.assertEqual(state["scenario"]["emission_mode"], "electric")
        self.assertEqual(state["scenario"]["demand_scale"], 1.3)

    def test_empty_scenario_does_not_run_solver(self):
        fake = FakeModel([{"intent": "scenario", "scenario": {}}])
        with patch("agent.graph.run_scenario") as run:
            graph = build_graph(fake)
            state = graph.invoke(
                {"messages": [HumanMessage(content="what happen if we change trucks?")]},
                {"configurable": {"thread_id": "empty-scenario"}},
            )

        run.assert_not_called()
        self.assertIn("I didn't find anything to change.", state["reply"])

    def test_value_equal_to_current_does_not_run_solver(self):
        fake = FakeModel([
            {"intent": "scenario", "scenario": {"emission_mode": "electric"}},
            {"intent": "scenario", "scenario": {"emission_mode": "electric"}},
        ])
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "equal-value"}}
            graph.invoke({"messages": [HumanMessage(content="Use electric trucks")]}, config)
            state = graph.invoke({"messages": [HumanMessage(content="Use electric trucks again")]}, config)

        self.assertEqual(run.call_count, 1)
        self.assertIn("Already electric trucks, nothing changes.", state["reply"])

    def test_percentage_demand_change_uses_demand_scale_and_has_readable_validation(self):
        fake = FakeModel([
            {"intent": "scenario", "scenario": {"demand_scale": 4.0}},
            {"intent": "scenario", "scenario": {"demand_scale": 1.3}},
        ])

        def validation(scenario):
            errors = []
            field_errors = []
            if scenario.demand_scale > 3:
                errors.append("demand_scale must be between 0.5 and 3")
                field_errors.append({"field": "demand_scale", "message": errors[-1]})
            if scenario.capacity_scale > 2:
                errors.append("capacity_scale must be between 0.5 and 2")
                field_errors.append({"field": "capacity_scale", "message": errors[-1]})
            if errors:
                return {"valid": False, "details": field_errors, "field_errors": field_errors, "warnings": []}
            return valid_scenario(scenario)

        with patch("agent.graph.validate_scenario", side_effect=validation), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "percentage-demand"}}
            first = graph.invoke(
                {"messages": [HumanMessage(content="Increase demand by 300% ")]}, config
            )
            second = graph.invoke(Command(resume="Increase demand by 30%"), config)

        self.assertTrue(first["__interrupt__"])
        self.assertIn("demand_scale", first["scenario"])
        self.assertNotIn("errors.pydantic.dev", str(first["__interrupt__"]))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(second["scenario"]["demand_scale"], 1.3)
        self.assertEqual(second["scenario"]["capacity_scale"], 1.0)

    def test_city_name_resolves_to_candidate_site_id(self):
        fake = FakeModel([{"intent": "scenario", "scenario": {"close_sites": ["Hamburg"]}}])
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            graph.invoke(
                {"messages": [HumanMessage(content="What if we close Hamburg site?")]},
                {"configurable": {"thread_id": "city-site"}},
            )
        self.assertEqual(run.call_args.args[0]["close_sites"], ["W01"])

    def test_hypothetical_site_question_runs_requested_scenario(self):
        fake = FakeModel([{"intent": "scenario", "scenario": {"close_sites": ["Hamburg"]}}])
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.run_scenario", side_effect=solved_result
        ) as run:
            graph = build_graph(fake)
            result = graph.invoke(
                {"messages": [HumanMessage(content="What happens if we close Hamburg site?")]},
                {"configurable": {"thread_id": "hypothetical-site-question"}},
            )

        self.assertNotIn("__interrupt__", result)
        self.assertEqual(run.call_args.args[0]["close_sites"], ["W01"])

    def test_unknown_city_is_not_silently_dropped_and_solved(self):
        fake = FakeModel([{"intent": "scenario", "scenario": {"close_sites": ["London"]}}])

        def validation(scenario):
            if "London" in scenario.close_sites:
                return {"valid": False, "details": [{"message": "unknown site: London"}], "warnings": []}
            return valid_scenario(scenario)

        with patch("agent.graph.validate_scenario", side_effect=validation), patch(
            "agent.graph.run_scenario"
        ) as run:
            graph = build_graph(fake)
            result = graph.invoke(
                {"messages": [HumanMessage(content="What if we close London site?")]},
                {"configurable": {"thread_id": "unknown-city-no-solve"}},
            )

        self.assertTrue(result["__interrupt__"])
        self.assertIn("London", result["scenario"]["close_sites"])
        run.assert_not_called()

    def test_unknown_site_interrupts_and_resumes_after_answer(self):
        def structured(messages):
            text = messages[-1].content.split("User message:")[-1]
            site = "W01" if "W01" in text else "W99"
            return {"intent": "scenario", "scenario": {"close_sites": [site]}}

        fake = FakeModel(structured)

        def validation(scenario):
            if "W99" in scenario.close_sites:
                return {"valid": False, "details": [{"message": "W99 is not a candidate site."}], "warnings": []}
            return valid_scenario(scenario)

        with patch("agent.graph.validate_scenario", side_effect=validation), patch(
            "agent.graph.get_baseline", return_value=BASELINE
        ), patch("agent.graph.run_scenario", side_effect=solved_result):
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "unknown-site"}}
            first = graph.invoke({"messages": [HumanMessage(content="Close W99")]}, config)
            second = graph.invoke(Command(resume="Use W01 instead"), config)
        self.assertTrue(first["__interrupt__"])
        self.assertEqual(second["scenario"]["close_sites"], ["W01"])
        self.assertNotIn("__interrupt__", second)

    def test_infeasible_result_proposes_fix_and_yes_reruns(self):
        fake = FakeModel([{"intent": "scenario", "scenario": {"k": 2}}])
        infeasible = {
            "ok": False,
            "status": "infeasible",
            "error_type": "infeasible",
            "details": ["two warehouses cannot absorb a failure"],
            "suggested_fix": {"field": "k", "value": 5},
        }
        optimal = solved_result({"objective": "co2", "k": 5})
        with patch("agent.graph.validate_scenario", side_effect=valid_scenario), patch(
            "agent.graph.get_baseline", return_value=BASELINE
        ), patch("agent.graph.run_scenario", side_effect=[infeasible, optimal]) as run:
            graph = build_graph(fake)
            config = {"configurable": {"thread_id": "infeasible"}}
            first = graph.invoke({"messages": [HumanMessage(content="Use 2 warehouses")]}, config)
            second = graph.invoke(Command(resume="yes"), config)
        self.assertTrue(first["__interrupt__"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(second["last_result"]["status"], "optimal")

    def test_number_check_regenerates_then_uses_structured_template(self):
        fake = FakeModel([], replies=["There are 999 warehouses.", "Still 999 warehouses."])
        result = solved_result({"objective": "co2", "k": 3})
        reply = _explain(fake, result, {"parameters_changed": []})
        self.assertNotIn("999", reply)
        self.assertEqual(fake.invoke_calls, 2)
        self.assertIn("100", reply)

    def test_out_of_scope_never_calls_run_scenario(self):
        fake = FakeModel([{"intent": "out_of_scope", "scenario": {}}], replies=["Routing is outside this tool's scope."])
        with patch("agent.graph.run_scenario") as run:
            result = ask("Plan tomorrow's truck routes", chat_model=fake)
        run.assert_not_called()
        self.assertEqual(result["error_type"], "out_of_scope")
        self.assertIn("outside", result["message"])


if __name__ == "__main__":
    unittest.main()
