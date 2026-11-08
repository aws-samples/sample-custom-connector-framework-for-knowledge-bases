"""Starting, describing, and stopping executions, and what a start does when the name exists."""

from __future__ import annotations

from typing import ClassVar

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import BaseModel

from common.errors import DependencyThrottledError, DependencyUnavailableError
from common.workflow_executions import ExistingExecution, WorkflowExecutions

STATE_MACHINE_ARN = "arn:aws:states:us-east-1:444455556666:stateMachine:CcfDestinationWorkflow"
EXECUTION_ARN = "arn:aws:states:us-east-1:444455556666:execution:CcfDestinationWorkflow:dst-1-create-1"


class ReusingInput(BaseModel):
    EXISTING_EXECUTION: ClassVar[ExistingExecution] = ExistingExecution.REUSE
    execution_name: str = "dst-1-create-1"


class FailingInput(ReusingInput):
    EXISTING_EXECUTION: ClassVar[ExistingExecution] = ExistingExecution.FAIL


def executions() -> tuple[WorkflowExecutions, Stubber]:
    client = boto3.client("stepfunctions", region_name="us-east-1")
    return WorkflowExecutions(client, STATE_MACHINE_ARN), Stubber(client)


def test_start_given_an_existing_name_and_a_reusing_input_then_returns_that_execution() -> None:
    workflows, stubber = executions()
    stubber.add_client_error("start_execution", service_error_code="ExecutionAlreadyExists")

    with stubber:
        assert workflows.start(ReusingInput()) == EXECUTION_ARN


def test_start_given_an_existing_name_and_a_failing_input_then_raises_unavailable() -> None:
    workflows, stubber = executions()
    stubber.add_client_error("start_execution", service_error_code="ExecutionAlreadyExists")

    with stubber, pytest.raises(DependencyUnavailableError):
        workflows.start(FailingInput())


def test_start_sends_the_name_and_the_input_json() -> None:
    workflows, stubber = executions()
    stubber.add_response(
        "start_execution",
        {"executionArn": EXECUTION_ARN, "startDate": "2026-11-03T00:00:00Z"},
        {
            "stateMachineArn": STATE_MACHINE_ARN,
            "name": "dst-1-create-1",
            "input": '{"execution_name":"dst-1-create-1"}',
        },
    )

    with stubber:
        assert workflows.start(ReusingInput()) == EXECUTION_ARN


def test_is_running_given_an_execution_step_functions_no_longer_has_then_returns_false() -> None:
    workflows, stubber = executions()
    stubber.add_client_error("describe_execution", service_error_code="ExecutionDoesNotExist")

    with stubber:
        assert workflows.is_running(EXECUTION_ARN) is False


def test_is_running_given_throttling_then_raises_the_throttling_error() -> None:
    workflows, stubber = executions()
    stubber.add_client_error("describe_execution", service_error_code="ThrottlingException")

    with stubber, pytest.raises(DependencyThrottledError):
        workflows.is_running(EXECUTION_ARN)


def test_stop_stops_the_execution_of_that_name() -> None:
    workflows, stubber = executions()
    stubber.add_response("stop_execution", {"stopDate": "2026-11-03T00:00:00Z"}, {"executionArn": EXECUTION_ARN})

    with stubber:
        assert workflows.stop("dst-1-create-1") is True


def test_stop_given_no_execution_of_that_name_then_returns_false() -> None:
    workflows, stubber = executions()
    stubber.add_client_error("stop_execution", service_error_code="ExecutionDoesNotExist")

    with stubber:
        assert workflows.stop("dst-1-create-1") is False
