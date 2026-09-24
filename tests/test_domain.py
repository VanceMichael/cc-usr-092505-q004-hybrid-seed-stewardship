import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hybrid_seed_stewardship.domain import load_domain

class DomainDataTest(unittest.TestCase):
    def test_example_matches_project(self) -> None:
        value = load_domain(Path('fixtures/domain.json'))
        self.assertEqual(value['domain'], 'hybrid-seed-stewardship')
        self.assertGreaterEqual(len(value['constraints']), 3)

if __name__ == '__main__':
    unittest.main()
