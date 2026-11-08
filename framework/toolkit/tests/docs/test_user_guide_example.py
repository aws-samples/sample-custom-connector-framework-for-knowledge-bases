"""The user guide's minimal connector implementation runs as written against the testing module."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from ccf_api_models.job_container import JobExitCode

from custom_connector_framework import ConnectorImplementation
from custom_connector_framework.testing import (
    RunImplementationRequest,
    SeedDocument,
    SeedState,
    TestRun,
    run_implementation,
)

USER_GUIDE = Path(__file__).resolve().parents[4] / "docs" / "user-guide.md"
EXAMPLE_BLOCK = re.compile(r"<!-- user-guide-example -->\s*```python\n(?P<code>.*?)```", re.DOTALL)
CONNECTOR_ID = RunImplementationRequest.model_fields["connector_id"].default
NOTES = {
    "notes": [
        {"id": "note-1", "title": "First note", "author": "ana", "text": "# First\nHello."},
        {"id": "note-2", "title": "Second note", "author": "li", "text": "# Second\nHi again."},
    ]
}


def load_example(directory: Path) -> type[ConnectorImplementation]:
    """Write the guide's example block to a module file, import it, and return its `NotesConnector`."""
    match = EXAMPLE_BLOCK.search(USER_GUIDE.read_text(encoding="utf-8"))
    assert match is not None, "docs/user-guide.md has no block after <!-- user-guide-example -->"
    module_path = directory / "user_guide_example.py"
    module_path.write_text(match.group("code"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("user_guide_example", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    implementation: type[ConnectorImplementation] = module.NotesConnector
    return implementation


@pytest.fixture
def example_run(tmp_path: Path) -> TestRun:
    return run_implementation(
        RunImplementationRequest(
            implementation=load_example(tmp_path),
            connector_configuration=json.dumps(NOTES),
            seed=SeedState(documents=(SeedDocument(document_id="note-0"),)),
        )
    )


def test_user_guide_example_records_every_note(example_run: TestRun) -> None:
    assert sorted(document.document_id for document in example_run.documents) == ["note-1", "note-2"]


def test_user_guide_example_deletes_a_stored_note_that_is_gone(example_run: TestRun) -> None:
    assert (example_run.report.total.written, example_run.report.total.deleted) == (2, 1)


def test_user_guide_example_writes_files_under_the_labeled_prefix(example_run: TestRun) -> None:
    prefix = f"connector_id={CONNECTOR_ID}/collection_id=col-default/document_id=doc-"
    note_files = [key for key in example_run.files if key.endswith("/note-1.md")]
    assert len(note_files) == 1
    assert note_files[0].startswith(prefix)
    assert f"{note_files[0]}.metadata.json" in example_run.files


def test_user_guide_example_logs_the_note_count(example_run: TestRun) -> None:
    events = [event for event in example_run.log_events if event.message == "notes_read"]
    assert [json.loads(event.line).get("notes") for event in events] == [2]


def test_user_guide_example_fails_without_configuration(tmp_path: Path) -> None:
    run = run_implementation(RunImplementationRequest(implementation=load_example(tmp_path)))

    assert run.exit_code is JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
    [failed] = [event for event in run.log_events if event.message == "connector_implementation_failed"]
    assert failed.stack_trace is not None and "needs a connector configuration" in failed.stack_trace
