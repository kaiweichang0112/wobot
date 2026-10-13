import json
import logging

from wobot.logs import JsonFormatter, TextFormatter


def test_json_lines_carry_severity_message_and_extra_fields():
    record = logging.makeLogRecord(
        {
            "levelname": "INFO",
            "msg": "read %d bytes",
            "args": (120,),
            "run_id": "run-1",
            "stage": "fetched",
        }
    )

    entry = json.loads(JsonFormatter().format(record))

    assert entry == {
        "severity": "INFO",
        "message": "read 120 bytes",
        "run_id": "run-1",
        "stage": "fetched",
    }


def test_terminal_lines_show_the_extra_fields_too():
    record = logging.makeLogRecord(
        {"levelname": "INFO", "msg": "turn finished", "route": "chat", "total_s": 1.2}
    )

    assert TextFormatter().format(record) == "INFO turn finished route=chat total_s=1.2"
