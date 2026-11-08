"""Starts, describes, and stops the executions of one Step Functions state machine.

Each workflow's input model names its execution and says what a start does when an execution of
that name already exists: a destination's create or delete reuses it, because a retried request
names the execution the first call started; a custom connector deletion fails, as a failed start does.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol

from botocore.exceptions import ClientError

from common.aws_error_code import AwsErrorCode
from common.errors import FailedAwsCall

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions import SFNClient


class ExecutionStatus(StrEnum):
    """A Step Functions execution's status, as `DescribeExecution` reports it."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ABORTED = "ABORTED"
    PENDING_REDRIVE = "PENDING_REDRIVE"


class ExistingExecution(StrEnum):
    REUSE = "REUSE"
    """The execution of this name is the one this input started: return its ARN."""
    FAIL = "FAIL"
    """Raise `DependencyUnavailableError`, as a failed start does."""


class WorkflowInput(Protocol):
    EXISTING_EXECUTION: ClassVar[ExistingExecution]

    @property
    def execution_name(self) -> str: ...

    def model_dump_json(self) -> str: ...


class WorkflowExecutions:
    def __init__(self, client: SFNClient, state_machine_arn: str) -> None:
        self._client = client
        self._state_machine_arn = state_machine_arn

    def start(self, workflow_input: WorkflowInput) -> str:
        """Start an execution and return its ARN."""
        name = workflow_input.execution_name
        try:
            response = self._client.start_execution(
                stateMachineArn=self._state_machine_arn, name=name, input=workflow_input.model_dump_json()
            )
        except ClientError as error:
            exists = AwsErrorCode.from_client_error(error) is AwsErrorCode.EXECUTION_ALREADY_EXISTS
            if exists and workflow_input.EXISTING_EXECUTION is ExistingExecution.REUSE:
                return self.execution_arn(name)
            raise FailedAwsCall(error=error, action="Workflow start").to_error() from error
        return response["executionArn"]

    def is_running(self, execution_arn: str) -> bool:
        """An execution Step Functions no longer has isn't running."""
        try:
            response = self._client.describe_execution(executionArn=execution_arn)
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is AwsErrorCode.EXECUTION_DOES_NOT_EXIST:
                return False
            raise FailedAwsCall(error=error, action="Workflow lookup").to_error() from error
        return ExecutionStatus(response["status"]) is ExecutionStatus.RUNNING

    def stop(self, execution_name: str) -> bool:
        """Stop the named execution; return False when Step Functions has no execution of that name."""
        try:
            self._client.stop_execution(executionArn=self.execution_arn(execution_name))
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is AwsErrorCode.EXECUTION_DOES_NOT_EXIST:
                return False
            raise FailedAwsCall(error=error, action="Workflow stop").to_error() from error
        return True

    def execution_arn(self, name: str) -> str:
        """`arn:<partition>:states:<region>:<account>:execution:<state machine>:<name>`."""
        return f"{self._state_machine_arn.replace(':stateMachine:', ':execution:', 1)}:{name}"
