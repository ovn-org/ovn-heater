import tempfile
import unittest
from pathlib import Path

import translate_yaml


class TranslateYamlTest(unittest.TestCase):
    def test_write_yaml_preserves_section_order(self):
        sections = {
            "global": {},
            "cluster": {},
            "base_cluster_bringup": {},
            "advertised_route": {},
        }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "scenario.yml"
            translate_yaml.write_yaml(sections, output)
            written = translate_yaml.load_yaml(output)

        self.assertEqual(list(written), list(sections))


if __name__ == "__main__":
    unittest.main()
