import json
import logging

from wobot.logs import JsonFormatter


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
