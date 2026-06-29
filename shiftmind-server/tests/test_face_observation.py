from types import SimpleNamespace

import numpy as np
import pytest

import face_recognition_service as module


class _Client:
    def __init__(self):
        self.rows = []

    def upsert(self, *, collection_name, data):
        assert collection_name == module.COLLECTION
        self.rows.extend(data)


class _Storage:
    def __init__(self):
        self.customer = SimpleNamespace(id="person-001", name="Ada")
        self.images = []

    def get_customer(self, customer_id):
        return self.customer if customer_id == self.customer.id else None

    def add_face_image(self, *, customer_id, image_bytes, quality):
        self.images.append((customer_id, image_bytes, quality))
        return "face-new", "/tmp/face-new.jpg"


def test_add_face_observation_inserts_another_embedding(monkeypatch):
    storage = _Storage()
    client = _Client()
    service = module.FaceRecognitionService.__new__(module.FaceRecognitionService)
    service.client = client
    monkeypatch.setattr(module, "storage", storage)
    monkeypatch.setattr(service, "embed_jpeg", lambda _: (np.ones(module.EMBED_DIM, dtype=np.float32), 0.91))
    monkeypatch.setattr(service, "vector_count", lambda: len(client.rows))

    result = service.add_face_observation(customer_id="person-001", crop_jpeg=b"jpeg")

    assert storage.images == [("person-001", b"jpeg", 0.91)]
    assert client.rows[0]["customer_id"] == "person-001"
    assert client.rows[0]["vector"] == [1.0] * module.EMBED_DIM
    assert result["face_image_id"] == "face-new"
    assert result["milvus_count"] == 1


def test_recognition_margin_ignores_second_embedding_for_same_customer(monkeypatch):
    service = module.FaceRecognitionService.__new__(module.FaceRecognitionService)
    service._votes = module.deque(maxlen=module.VOTE_WINDOW)
    monkeypatch.setattr(service, "embed_jpeg", lambda _: (np.ones(module.EMBED_DIM), 0.9))
    monkeypatch.setattr(service, "_search", lambda _: [
        {"customer_id": "person-001", "score": 0.82},
        {"customer_id": "person-001", "score": 0.80},
        {"customer_id": "person-002", "score": 0.61},
    ])
    events = []
    monkeypatch.setattr(module.storage, "add_recognition_event", lambda **event: events.append(event))

    result = service.recognize(b"jpeg")

    assert result.status == "candidate"
    assert result.person_id == "person-001"
    assert result.margin == pytest.approx(0.21)
    assert events[0]["candidate_customer_id"] == "person-001"
