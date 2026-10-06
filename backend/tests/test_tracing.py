from langsmith.utils import tracing_is_enabled


def test_tests_send_no_traces():
    assert tracing_is_enabled() is False
