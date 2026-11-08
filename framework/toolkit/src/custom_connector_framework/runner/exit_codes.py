"""The failure before `run()` that ends a job with its exit code.

The codes are the job container contract's `JobExitCode`. The job state machine reads the
`connector` container's exit code and builds the job's `status_reason` from it, so the code is
the only thing about a failure that reaches the service account.
"""

from ccf_api_models.job_container import JobExitCode


class JobStartError(Exception):
    """A failure before `run()` that ends the job with its exit code."""

    exit_code: JobExitCode = JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
