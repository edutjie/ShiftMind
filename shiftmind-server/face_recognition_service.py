"""InsightFace + Milvus Lite recognition service."""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException

from storage import DATA_DIR, storage

COLLECTION = "face_embeddings"
MODEL_NAME = "buffalo_l"
EMBED_DIM = 512
SIMILARITY_THRESHOLD = 0.50
MARGIN_THRESHOLD = 0.05
VOTE_WINDOW = 5
CONFIRM_VOTES = 3
VOTE_TTL_SECONDS = 2.0


def normalize_face_crop_jpeg(jpeg: bytes) -> bytes:
    """Match the server preview orientation for ML Kit face crops."""
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    rotated = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    ok, buf = cv2.imencode(".jpg", rotated, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return buf.tobytes() if ok else jpeg


@dataclass
class RecognitionResult:
    status: str
    person_id: str | None = None
    name: str | None = None
    similarity: float | None = None
    margin: float | None = None
    vote_count: int = 0
    threshold: float = SIMILARITY_THRESHOLD
    summary: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "recognition_status": self.status,
            "person_id": self.person_id,
            "name": self.name,
            "similarity": self.similarity,
            "margin": self.margin,
            "vote_count": self.vote_count,
            "threshold": self.threshold,
            "summary": self.summary,
            "reason": self.reason,
        }


class FaceRecognitionService:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "milvus").mkdir(parents=True, exist_ok=True)
        self.client = MilvusClient(uri=str(DATA_DIR / "milvus" / "shiftmind.db"))
        self._model: Any | None = None
        self._votes: deque[tuple[float, str | None, bool, float | None, float | None]] = deque(maxlen=VOTE_WINDOW)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if self.client.has_collection(COLLECTION):
            self.client.load_collection(COLLECTION)
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("embedding_id", DataType.VARCHAR, is_primary=True, max_length=80)
        schema.add_field("customer_id", DataType.VARCHAR, max_length=80)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBED_DIM)
        schema.add_field("model_name", DataType.VARCHAR, max_length=40)
        schema.add_field("quality", DataType.FLOAT)
        schema.add_field("created_at", DataType.INT64)
        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(collection_name=COLLECTION, schema=schema, index_params=index_params)
        self.client.load_collection(COLLECTION)

    @property
    def model(self) -> Any:
        if self._model is None:
            from insightface.app import FaceAnalysis

            app = FaceAnalysis(name=MODEL_NAME, providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=0, det_size=(640, 640))
            self._model = app
        return self._model

    def embed_jpeg(self, jpeg: bytes) -> tuple[np.ndarray, float]:
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("decode_failed")
        faces = self.model.get(img)
        if not faces:
            raise ValueError("no_face")
        face = max(faces, key=lambda f: _bbox_area(getattr(f, "bbox", None)))
        emb = np.asarray(face.embedding, dtype=np.float32)
        norm = np.linalg.norm(emb)
        if norm <= 0:
            raise ValueError("empty_embedding")
        quality = float(getattr(face, "det_score", 0.0) or 0.0)
        return emb / norm, quality

    def enroll_latest_crop(self, *, customer_id: str, name: str, memory_card: str, crop_jpeg: bytes) -> dict[str, Any]:
        customer = storage.upsert_customer(
            customer_id=customer_id,
            name=name,
            memory_card=memory_card,
            preferences=[],
        )
        return self.add_face_observation(customer_id=customer.id, crop_jpeg=crop_jpeg)

    def add_face_observation(self, *, customer_id: str, crop_jpeg: bytes) -> dict[str, Any]:
        """Store another real-session face sample for an existing customer."""
        customer = storage.get_customer(customer_id)
        if not customer:
            raise KeyError(customer_id)
        embedding, quality = self.embed_jpeg(crop_jpeg)
        face_image_id, image_path = storage.add_face_image(
            customer_id=customer.id,
            image_bytes=crop_jpeg,
            quality=quality,
        )
        embedding_id = f"emb-{uuid.uuid4().hex}"
        self.client.upsert(
            collection_name=COLLECTION,
            data=[{
                "embedding_id": embedding_id,
                "customer_id": customer.id,
                "vector": embedding.tolist(),
                "model_name": MODEL_NAME,
                "quality": quality,
                "created_at": int(time.time()),
            }],
        )
        return {
            "customer_id": customer.id,
            "name": customer.name,
            "embedding_id": embedding_id,
            "face_image_id": face_image_id,
            "image_path": image_path,
            "quality": quality,
            "milvus_count": self.vector_count(),
        }

    def recognize(self, jpeg: bytes) -> RecognitionResult:
        try:
            embedding, _ = self.embed_jpeg(jpeg)
        except ValueError as exc:
            result = RecognitionResult(status="no_face", reason=str(exc))
            storage.add_recognition_event(status=result.status, candidate_customer_id=None, similarity=None, margin=None)
            return result

        hits = self._search(embedding)
        if not hits:
            result = self._vote(None, False, None, None)
            storage.add_recognition_event(status=result.status, candidate_customer_id=None, similarity=None, margin=None)
            return result

        top1 = hits[0]
        # Multiple session observations may occupy the nearest slots. Margin
        # must compare identities, not two embeddings belonging to one person.
        top2_score = next(
            (hit["score"] for hit in hits[1:] if hit["customer_id"] != top1["customer_id"]),
            0.0,
        )
        similarity = float(top1["score"])
        margin = float(similarity - top2_score)
        customer_id = str(top1["customer_id"])
        valid = similarity >= SIMILARITY_THRESHOLD and margin >= MARGIN_THRESHOLD
        result = self._vote(customer_id, valid, similarity, margin)
        storage.add_recognition_event(
            status=result.status,
            candidate_customer_id=customer_id if valid else None,
            similarity=similarity,
            margin=margin,
        )
        return result

    def _search(self, embedding: np.ndarray) -> list[dict[str, Any]]:
        if self.vector_count() == 0:
            return []
        raw = self.client.search(
            collection_name=COLLECTION,
            data=[embedding.tolist()],
            anns_field="vector",
            limit=5,
            output_fields=["customer_id", "embedding_id"],
            search_params={"metric_type": "COSINE"},
        )
        hits = raw[0] if raw else []
        parsed: list[dict[str, Any]] = []
        for hit in hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else getattr(hit, "entity", {})
            distance = hit.get("distance") if isinstance(hit, dict) else getattr(hit, "distance", None)
            # Milvus Lite / pymilvus 3 returns COSINE as distance where 0.0 is
            # identical. The recognition thresholds use similarity semantics.
            similarity = max(-1.0, min(1.0, 1.0 - float(distance if distance is not None else 1.0)))
            parsed.append({
                "customer_id": entity.get("customer_id") if isinstance(entity, dict) else entity.customer_id,
                "embedding_id": entity.get("embedding_id") if isinstance(entity, dict) else entity.embedding_id,
                "score": similarity,
            })
        return parsed

    def _vote(self, customer_id: str | None, valid: bool, similarity: float | None, margin: float | None) -> RecognitionResult:
        now = time.monotonic()
        self._votes.append((now, customer_id, valid, similarity, margin))
        fresh = [v for v in self._votes if now - v[0] <= VOTE_TTL_SECONDS]
        self._votes = deque(fresh[-VOTE_WINDOW:], maxlen=VOTE_WINDOW)
        counts: dict[str, int] = {}
        for _, cid, ok, _, _ in self._votes:
            if ok and cid:
                counts[cid] = counts.get(cid, 0) + 1
        if counts:
            winner, vote_count = max(counts.items(), key=lambda item: item[1])
            if vote_count >= CONFIRM_VOTES:
                customer = storage.get_customer(winner)
                return RecognitionResult(
                    status="confirmed",
                    person_id=winner,
                    name=customer.name if customer else winner,
                    similarity=similarity,
                    margin=margin,
                    vote_count=vote_count,
                    summary=(customer.memory_card if customer and customer.memory_card else "Dummy summary: returning customer."),
                )
        if len(self._votes) >= VOTE_WINDOW:
            return RecognitionResult(
                status="unrecognized_first_visit",
                similarity=similarity,
                margin=margin,
                vote_count=0,
                summary="unrecognized, first visit",
            )
        return RecognitionResult(
            status="candidate" if valid else "searching",
            person_id=customer_id if valid else None,
            similarity=similarity,
            margin=margin,
            vote_count=counts.get(customer_id or "", 0),
            summary="recognizing",
        )

    def vector_count(self) -> int:
        try:
            stats = self.client.get_collection_stats(COLLECTION)
            return int(stats.get("row_count", 0))
        except Exception:
            return 0

    def diagnostics(self) -> dict[str, Any]:
        return {
            "collection": COLLECTION,
            "model_name": MODEL_NAME,
            "embedding_dim": EMBED_DIM,
            "milvus_count": self.vector_count(),
            "threshold": SIMILARITY_THRESHOLD,
            "margin_threshold": MARGIN_THRESHOLD,
            "vote_window": VOTE_WINDOW,
            "confirm_votes": CONFIRM_VOTES,
        }

    def list_embeddings(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if self.vector_count() == 0:
            return []
        rows = self.client.query(
            collection_name=COLLECTION,
            filter='embedding_id != ""',
            output_fields=["embedding_id", "customer_id", "vector", "model_name", "quality", "created_at"],
            limit=limit,
        )
        if hasattr(rows, "to_dict"):
            data = rows.to_dict().get("data", [])
            return data if isinstance(data, list) else []
        return rows if isinstance(rows, list) else []

    def embedding_projection(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.list_embeddings(limit=limit)
        vectors = [np.asarray(row.get("vector", []), dtype=np.float32) for row in rows]
        valid = [(row, vec) for row, vec in zip(rows, vectors) if vec.shape == (EMBED_DIM,)]
        if not valid:
            return []
        if len(valid) == 1:
            row, vec = valid[0]
            return [{**_embedding_meta(row, vec), "x": 0.0, "y": 0.0}]

        matrix = np.stack([vec for _, vec in valid])
        matrix = matrix - matrix.mean(axis=0, keepdims=True)
        _, _, vh = np.linalg.svd(matrix, full_matrices=False)
        coords = matrix @ vh[:2].T
        return [
            {**_embedding_meta(row, vec), "x": float(coord[0]), "y": float(coord[1])}
            for (row, vec), coord in zip(valid, coords)
        ]


def _bbox_area(bbox: Any) -> float:
    if bbox is None:
        return 0.0
    return float(max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1]))


def _embedding_meta(row: dict[str, Any], vector: np.ndarray) -> dict[str, Any]:
    return {
        "embedding_id": row.get("embedding_id"),
        "customer_id": row.get("customer_id"),
        "model_name": row.get("model_name"),
        "quality": row.get("quality"),
        "created_at": row.get("created_at"),
        "vector_norm": float(np.linalg.norm(vector)),
    }


_face_service: FaceRecognitionService | None = None


def get_face_service() -> FaceRecognitionService:
    """Lazy singleton so uvicorn reload can import routes without locking Milvus Lite."""
    global _face_service
    if _face_service is None:
        _face_service = FaceRecognitionService()
    return _face_service


def face_service_status() -> dict[str, Any]:
    try:
        return get_face_service().diagnostics()
    except (MilvusException, RuntimeError, OSError, Exception) as exc:
        return {
            "collection": COLLECTION,
            "model_name": MODEL_NAME,
            "embedding_dim": EMBED_DIM,
            "milvus_count": None,
            "threshold": SIMILARITY_THRESHOLD,
            "margin_threshold": MARGIN_THRESHOLD,
            "vote_window": VOTE_WINDOW,
            "confirm_votes": CONFIRM_VOTES,
            "error": f"{type(exc).__name__}: {exc}",
        }
