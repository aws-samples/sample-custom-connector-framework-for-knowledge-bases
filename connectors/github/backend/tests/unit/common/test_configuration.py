"""The owner and the listed repositories go into GitHub lookups, so only valid names pass."""

import pytest
from pydantic import ValidationError

from tests.configurations import make_configuration


@pytest.mark.parametrize("owner", ["aws-samples", "aws", "a" * 39, "AWS-Samples2"])
def test_a_github_login_is_accepted_as_the_owner(owner: str) -> None:
    configuration = make_configuration().model_copy(update={"owner": owner})

    assert type(configuration).model_validate_json(configuration.model_dump_json()).owner == owner


@pytest.mark.parametrize(
    "owner", ["aws-samples fork:true", "x org:other", "-aws", "a" * 40, "aws/samples", "aws_samples", ""]
)
def test_an_owner_that_isnt_a_github_login_is_rejected(owner: str) -> None:
    configuration = make_configuration().model_copy(update={"owner": owner})

    with pytest.raises(ValidationError, match="owner"):
        type(configuration).model_validate_json(configuration.model_dump_json())


@pytest.mark.parametrize("repository", ["git-secrets", "aws.tools", "a_b-c", "x" * 100])
def test_a_repository_name_is_accepted(repository: str) -> None:
    configuration = make_configuration().model_copy(update={"repositories": (repository,)})

    assert type(configuration).model_validate_json(configuration.model_dump_json()).repositories == (repository,)


@pytest.mark.parametrize("repository", ["awslabs/git-secrets", "", "x" * 101, "a b", "repo:other"])
def test_a_repository_that_isnt_a_name_without_its_owner_is_rejected(repository: str) -> None:
    configuration = make_configuration().model_copy(update={"repositories": (repository,)})

    with pytest.raises(ValidationError, match="repositories"):
        type(configuration).model_validate_json(configuration.model_dump_json())
