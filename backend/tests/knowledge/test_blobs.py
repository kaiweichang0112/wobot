from google.api_core.exceptions import PreconditionFailed

from wobot.knowledge.blobs import GcsBlobStore, LocalBlobStore, blob_key


class FakeBucket:
    """Just enough of a Cloud Storage bucket: objects by name, and the create-only upload."""

    def __init__(self, objects=None, lose_the_race=False):
        self.objects = dict(objects or {})
        self.uploads = []
        self.lose_the_race = lose_the_race

    def blob(self, name):
        return FakeBlob(self, name)


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def upload_from_string(self, content, *, content_type, if_generation_match):
        self.bucket.uploads.append((self.name, if_generation_match))
        if self.bucket.lose_the_race:
            raise PreconditionFailed("object already exists")
        self.bucket.objects[self.name] = content


async def test_local_blobs_are_stored_once_under_their_hash(tmp_path):
    blobs = LocalBlobStore(tmp_path)

    key = await blobs.put(b"catalog bytes")
    again = await blobs.put(b"catalog bytes")

    assert key == again == blob_key(b"catalog bytes")
    assert (tmp_path / key).read_bytes() == b"catalog bytes"
    assert [path.name for path in tmp_path.rglob("*.tmp")] == []


async def test_gcs_creates_the_object_and_never_replaces_one():
    bucket = FakeBucket()

    key = await GcsBlobStore(bucket).put(b"catalog bytes")

    assert bucket.objects == {key: b"catalog bytes"}
    assert bucket.uploads == [(blob_key(b"catalog bytes"), 0)]  # generation 0: create only


async def test_gcs_skips_bytes_already_stored():
    key = blob_key(b"catalog bytes")
    bucket = FakeBucket({key: b"catalog bytes"})

    assert await GcsBlobStore(bucket).put(b"catalog bytes") == key
    assert bucket.uploads == []


async def test_gcs_treats_a_lost_race_as_stored():
    bucket = FakeBucket(lose_the_race=True)

    assert await GcsBlobStore(bucket).put(b"catalog bytes") == blob_key(b"catalog bytes")
