"""Models and contracts shared by every CCF project.

`make -C framework/api build` generates `models.py` (datamodel-code-generator), `operations.py` and
`metric_names.py` (`codegen/write_enums.py`), and copies `openapi.json` from the Smithy build. The
hand-written modules are `base.py`, `catalog.py`, `identifiers.py`, and `contracts/`.
"""
