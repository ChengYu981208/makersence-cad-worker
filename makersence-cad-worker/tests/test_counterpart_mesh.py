import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "heavy"))
from counterpart_mesh import load_3mf_assembly_mesh


MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="centimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model"><mesh>
      <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/></vertices>
      <triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/><triangle v1="1" v2="2" v3="3"/><triangle v1="2" v2="0" v3="3"/></triangles>
    </mesh></object>
    <object id="2" type="model"><mesh>
      <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/></vertices>
      <triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/><triangle v1="1" v2="2" v3="3"/><triangle v1="2" v2="0" v3="3"/></triangles>
    </mesh></object>
    <object id="3" type="model"><components>
      <component objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
      <component objectid="2" transform="1 0 0 0 1 0 0 0 1 2 0 0"/>
    </components></object>
  </resources>
  <build><item objectid="3"/></build>
</model>
"""


class ThreeMFAssemblyMeshTests(unittest.TestCase):
    def make_model(self, xml=MODEL):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "assembly.3mf"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("3D/3dmodel.model", xml)
        return str(path)

    def test_expands_components_and_applies_unit_and_component_transforms(self):
        mesh = load_3mf_assembly_mesh(self.make_model())
        self.assertEqual(len(mesh["vertices"]), 8)
        self.assertEqual(len(mesh["triangles"]), 8)
        self.assertEqual(mesh["assembly"]["root_count"], 1)
        self.assertEqual(mesh["assembly"]["part_instance_count"], 2)
        self.assertEqual(mesh["assembly"]["source_structure"], "TRANSFORM_AWARE_3MF_BUILD_GRAPH")
        self.assertEqual(mesh["bbox"]["min"], [0.0, 0.0, 0.0])
        self.assertEqual(mesh["bbox"]["max"], [30.0, 10.0, 10.0])

    def test_rejects_assemblies_that_exceed_the_expanded_triangle_budget(self):
        with self.assertRaisesRegex(ValueError, "3MF_ALIGNMENT_EXPANDED_MESH_BUDGET_EXCEEDED"):
            load_3mf_assembly_mesh(self.make_model(), max_triangles=4)

    def test_buildless_model_uses_unreferenced_component_roots(self):
        xml = MODEL.replace("<build><item objectid=\\"3\\"/></build>", "<build/>")
        mesh = load_3mf_assembly_mesh(self.make_model(xml))
        self.assertEqual(mesh["assembly"]["root_count"], 1)
        self.assertEqual(mesh["assembly"]["part_instance_count"], 2)
        self.assertEqual(mesh["bbox"]["max"], [30.0, 10.0, 0.0])


if __name__ == "__main__":
    unittest.main()
