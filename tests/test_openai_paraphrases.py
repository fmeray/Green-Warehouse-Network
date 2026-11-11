import os
import unittest

from agent.graph import _chat_model, _structured_intent
from src.schema import scenario_from_dict


@unittest.skipUnless(os.getenv("OPENAI_API_KEY"), "requires OPENAI_API_KEY")
class OpenAIParaphraseTests(unittest.TestCase):
    """Live parsing smoke test; it is intentionally opt-in through the API key."""

    def test_fifteen_natural_language_paraphrases(self):
        cases = [
            ("what happen if we change trucks to electric?", "emission_mode", "electric"),
            ("switch the fleet to e-trucks", "emission_mode", "electric"),
            ("use battery-powered delivery vehicles", "emission_mode", "electric"),
            ("replace diesel with an electric truck fleet", "emission_mode", "electric"),
            ("what if customer demand grows by 30%?", "demand_scale", 1.3),
            ("increase yearly volume by thirty percent", "demand_scale", 1.3),
            ("assume demand is up 30 percent", "demand_scale", 1.3),
            ("what happens if Hamburg warehouse closes?", "close_sites", ["W01"]),
            ("decommission the Hamburg logistics hub", "close_sites", ["W01"]),
            ("remove Hamburg from the warehouse network", "close_sites", ["W01"]),
            ("keep the backup within 200 km", "max_backup_km", 200),
            ("limit backup coverage to 200 kilometers", "max_backup_km", 200),
            ("set the maximum backup distance at 200 km", "max_backup_km", 200),
            ("minimize annual operating cost", "objective", "cost"),
            ("optimize for the lowest spend", "objective", "cost"),
        ]
        model = _chat_model()
        current = scenario_from_dict({}).model_dump()
        correct = 0
        for text, field, expected in cases:
            with self.subTest(text=text):
                envelope = _structured_intent(model, text, current)
                actual = envelope.scenario.model_dump(exclude_none=True).get(field)
                if actual == expected:
                    correct += 1
                self.assertEqual(actual, expected)
        print(f"Parsed correctly: {correct}/{len(cases)}")


if __name__ == "__main__":
    unittest.main()
