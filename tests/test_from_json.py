# -*- coding: utf-8 -*-

"""Test the module can be imported."""

import unittest
import os
import yaml
from linkml_runtime.utils.schemaview import SchemaView

from linkml_model_enrichment.importers.json_instance_import_engine import JsonInstanceImportEngine
from linkml.generators.yamlgen import YAMLGenerator
from tests import INPUT_DIR, OUTPUT_DIR

IN = os.path.join(INPUT_DIR, 'synonymizer.yaml')
IN_GOLD = os.path.join(INPUT_DIR, 'neon-in-gold.json.gz')
OUTSCHEMA = os.path.join(OUTPUT_DIR, 'syn-schema.yaml')
OUTSCHEMA_GOLD = os.path.join(OUTPUT_DIR, 'neon-in-gold-inf.yaml')


class TestJsonImport(unittest.TestCase):
    """JSON """

    def test_from_json(self):
        """Test inference of a schema from JSON instance data (small example)."""
        ie = JsonInstanceImportEngine()
        schema_dict = ie.convert(IN, format='yaml')
        ys = yaml.dump(schema_dict, default_flow_style=False, sort_keys=False)
        print(ys)
        with open(OUTSCHEMA, 'w') as stream:
            stream.write(ys)
        s = YAMLGenerator(ys).serialize()
        with open(OUTSCHEMA_ENHANCED, 'w') as stream:
            stream.write(s)
        sv = SchemaView(ys)
        assert 'NewSynonym' in sv.get_enum(sv.induced_slot('type', 'Rules').range).permissible_values


    def test_gold_neon(self):
        """Test inference of a schema from JSON instance data (GOLD API example)."""
        ie = JsonInstanceImportEngine()
        schema_dict = ie.convert(IN_GOLD, format='json.gz')
        ys = yaml.dump(schema_dict, default_flow_style=False, sort_keys=False)
        print(ys)
        with open(OUTSCHEMA_GOLD, 'w') as stream:
            stream.write(ys)
        sv = SchemaView(ys)

