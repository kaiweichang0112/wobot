import hashlib

import httpx2
import pytest

from wobot.knowledge.sources.drive import DriveError, fetch_drive_file

CONTENT = b"catalog bytes"
METADATA = {
    "name": "catalog.xlsx",
    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "size": str(len(CONTENT)),
    "sha256Checksum": hashlib.sha256(CONTENT).hexdigest(),
    "modifiedTime": "2026-09-01T00:00:00.000Z",
}


def drive(metadata, content=CONTENT, status=200, requests=None) -> httpx2.AsyncClient:
    """A client whose Drive answers with this metadata and these bytes."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(request)
        if status != 200:
            return httpx2.Response(status, json={"error": {"code": status}})
        if request.url.params.get("alt") == "media":
            return httpx2.Response(200, content=content)
        return httpx2.Response(200, json=metadata)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_downloads_the_file_with_its_metadata():
    requests = []

    file = await fetch_drive_file(drive(METADATA, requests=requests), "file-id", "token")

    assert (file.name, file.content) == ("catalog.xlsx", CONTENT)
    assert file.sha256 == METADATA["sha256Checksum"]
    assert file.modified_time == "2026-09-01T00:00:00.000Z"
    assert [request.url.path for request in requests] == ["/drive/v3/files/file-id"] * 2
    assert all(request.headers["Authorization"] == "Bearer token" for request in requests)


async def test_rejects_bytes_that_do_not_match_the_checksum():
    with pytest.raises(DriveError, match="checksum"):
        await fetch_drive_file(drive(METADATA, content=b"cut short"), "file-id", "token")


async def test_rejects_a_google_sheet_which_has_no_stored_bytes():
    sheet = {"name": "catalog", "mimeType": "application/vnd.google-apps.spreadsheet"}

    with pytest.raises(DriveError, match="google-apps.spreadsheet is not a stored file"):
        await fetch_drive_file(drive(sheet), "file-id", "token")


async def test_a_file_that_is_not_shared_reads_as_not_found():
    with pytest.raises(DriveError, match="shared with the service account"):
        await fetch_drive_file(drive(METADATA, status=404), "file-id", "token")


async def test_refuses_a_file_far_larger_than_any_source():
    huge = {**METADATA, "size": str(10**9)}
    requests = []

    with pytest.raises(DriveError, match="limit"):
        await fetch_drive_file(drive(huge, requests=requests), "file-id", "token")
    assert len(requests) == 1  # never downloaded
