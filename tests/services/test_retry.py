from unittest.mock import Mock

import pytest

from ticket_reviewer.connectors.base import ConnectorFailure, FailureCategory
from ticket_reviewer.domain.enums import Source
from ticket_reviewer.services.retry import call_with_retry


def connector_failure(*, retryable=True, category=FailureCategory.NETWORK):
    return ConnectorFailure(Source.STUBHUB, category, "safe failure", retryable)


def test_retry_returns_immediately_on_success():
    operation = Mock(return_value="ok")
    sleep = Mock()

    assert call_with_retry(operation, sleep=sleep) == "ok"
    assert operation.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("failures_before_success", [1, 2])
def test_retry_recovers_on_later_attempt(failures_before_success):
    operation = Mock(
        side_effect=[connector_failure()] * failures_before_success + ["ok"]
    )
    sleep = Mock()

    assert call_with_retry(operation, sleep=sleep, attempts=3) == "ok"
    assert operation.call_count == failures_before_success + 1
    assert [call.args[0] for call in sleep.call_args_list] == [
        0.5,
        1.0,
    ][:failures_before_success]


def test_retry_stops_after_three_retryable_failures():
    operation = Mock(side_effect=connector_failure())
    sleep = Mock()

    with pytest.raises(ConnectorFailure):
        call_with_retry(operation, sleep=sleep, attempts=3)

    assert operation.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [0.5, 1.0]


@pytest.mark.parametrize(
    "failure",
    [
        connector_failure(category=FailureCategory.AUTH),
        connector_failure(category=FailureCategory.PARSE),
        connector_failure(category=FailureCategory.UNSUPPORTED),
        connector_failure(retryable=False),
    ],
)
def test_nonretryable_connector_failures_propagate_immediately(failure):
    operation = Mock(side_effect=failure)
    sleep = Mock()

    with pytest.raises(ConnectorFailure) as caught:
        call_with_retry(operation, sleep=sleep)

    assert caught.value is failure
    assert operation.call_count == 1
    sleep.assert_not_called()


def test_nonconnector_exception_propagates_immediately():
    operation = Mock(side_effect=RuntimeError("programming error"))
    sleep = Mock()

    with pytest.raises(RuntimeError, match="programming error"):
        call_with_retry(operation, sleep=sleep)

    assert operation.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("attempts", [True, False, 0, -1, 1.5, "3", None])
def test_attempts_must_be_a_positive_nonboolean_integer(attempts):
    with pytest.raises(ValueError, match="positive non-boolean integer"):
        call_with_retry(lambda: "ok", sleep=lambda _: None, attempts=attempts)


def test_additional_attempts_continue_exponential_backoff():
    operation = Mock(side_effect=connector_failure())
    sleep = Mock()

    with pytest.raises(ConnectorFailure):
        call_with_retry(operation, sleep=sleep, attempts=5)

    assert [call.args[0] for call in sleep.call_args_list] == [0.5, 1.0, 2.0, 4.0]
