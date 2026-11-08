"""The interface every connector implementation subclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod

from custom_connector_framework.job_context import JobContext
from custom_connector_framework.store.document_store import DocumentStore


class ConnectorImplementation(ABC):
    """Reads one source into the store. `ccf-connector run <module>:<class>` builds it once per job.

    The toolkit calls `run()` once. When it returns, the toolkit sends every queued record and
    the job succeeds; when it raises, the toolkit sends the queued puts, then the deletes, and
    the job fails.
    """

    def __init__(self, job: JobContext, store: DocumentStore) -> None:
        self.job = job
        self.store = store

    @abstractmethod
    def run(self) -> None:
        """Read the source and hand documents, deletes, and checkpoints to `self.store`."""
