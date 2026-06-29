"""Milvus Lite vector store of session summaries for the Q&A agent.

A second collection (`session_memories`) in the same Milvus DB file as
`face_embeddings`. Holds text embeddings of each ended session's
summary + important_points so the agent can do semantic/topical recall
("find sessions about returns", "what do clients usually ask about").
The face service remains the only store for face vectors.

Mirrors face_recognition_service.py's client/schema/index/search/singleton
pattern. Text embeddings use their own OpenAI-compatible client configuration,
with the shared/LLM settings retained as backward-compatible fallbacks.
"""
from __future__ import annotations

import os
import time
from typing import Any

from pymilvus import DataType, MilvusClient

from storage import DATA_DIR

COLLECTION = "session_memories"
MODEL_NAME = os.getenv("SHIFTMIND_EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = int(os.getenv("SHIFTMIND_EMBED_DIM", "1536"))
MAX_CONTENT_CHARS = 4000
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _embedding_client() -> Any | None:
    """Dedicated embeddings client with backward-compatible shared fallbacks."""
    api_key = (
        os.getenv("SHIFTMIND_EMBED_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("SHIFTMIND_LLM_API_KEY")
    )
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = (
        os.getenv("SHIFTMIND_EMBED_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or os.getenv("SHIFTMIND_LLM_BASE_URL")
    )
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


class SessionMemoryService:
    def __init__(self, uri: str | None = None) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "milvus").mkdir(parents=True, exist_ok=True)
        self.client = MilvusClient(uri=uri or str(DATA_DIR / "milvus" / "shiftmind.db"))
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if self.client.has_collection(COLLECTION):
            self.client.load_collection(COLLECTION)
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("memory_id", DataType.VARCHAR, is_primary=True, max_length=80)
        schema.add_field("session_id", DataType.VARCHAR, max_length=80)
        schema.add_field("customer_id", DataType.VARCHAR, max_length=80)
        schema.add_field("employee_id", DataType.VARCHAR, max_length=80)
        schema.add_field("location_id", DataType.VARCHAR, max_length=80)
        schema.add_field("content", DataType.VARCHAR, max_length=MAX_CONTENT_CHARS)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBED_DIM)
        schema.add_field("model_name", DataType.VARCHAR, max_length=40)
        schema.add_field("outcome", DataType.VARCHAR, max_length=20)
        schema.add_field("created_at", DataType.INT64)
        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(collection_name=COLLECTION, schema=schema, index_params=index_params)
        self.client.load_collection(COLLECTION)

    # ── embeddings ────────────────────────────────────────────────────────

    def embed_text(self, text: str) -> list[float]:
        """Embed text via the OpenAI-compatible API. Raises if unconfigured."""
        client = _embedding_client()
        if client is None:
            raise RuntimeError("missing_embedding_config")
        resp = client.embeddings.create(model=MODEL_NAME, input=text)
        return list(resp.data[0].embedding)

    # ── write ─────────────────────────────────────────────────────────────

    def upsert_memory(
        self,
        *,
        memory_id: str,
        session_id: str,
        customer_id: str,
        employee_id: str | None,
        location_id: str | None,
        content: str,
        outcome: str,
        started_at_iso: str = "",
    ) -> dict[str, Any]:
        vector = self.embed_text(content)
        self.client.upsert(
            collection_name=COLLECTION,
            data=[
                {
                    "memory_id": memory_id,
                    "session_id": session_id,
                    "customer_id": customer_id,
                    "employee_id": employee_id or "",
                    "location_id": location_id or "",
                    "content": content[:MAX_CONTENT_CHARS],
                    "vector": vector,
                    "model_name": MODEL_NAME,
                    "outcome": outcome,
                    "created_at": _iso_to_epoch(started_at_iso) or int(time.time()),
                }
            ],
        )
        return {"memory_id": memory_id, "session_id": session_id, "count": self.count()}

    # ── read ──────────────────────────────────────────────────────────────

    def search(
        self,
        *,
        query: str,
        limit: int = 5,
        employee_id: str | None = None,
        customer_id: str | None = None,
        location_id: str | None = None,
        created_at_min: int | None = None,
        created_at_max: int | None = None,
    ) -> list[dict[str, Any]]:
        if self.count() == 0:
            return []
        vector = self.embed_text(query)
        expr = _build_filter(
            employee_id=employee_id,
            customer_id=customer_id,
            location_id=location_id,
            created_at_min=created_at_min,
            created_at_max=created_at_max,
        )
        raw = self.client.search(
            collection_name=COLLECTION,
            data=[vector],
            anns_field="vector",
            limit=min(max(limit, 1), 20),
            filter=expr,
            output_fields=[
                "session_id",
                "customer_id",
                "employee_id",
                "location_id",
                "content",
                "outcome",
                "created_at",
            ],
            search_params={"metric_type": "COSINE"},
        )
        hits = raw[0] if raw else []
        parsed: list[dict[str, Any]] = []
        for hit in hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else getattr(hit, "entity", {})
            distance = hit.get("distance") if isinstance(hit, dict) else getattr(hit, "distance", None)
            # COSINE distance: 0.0 == identical (see face_recognition_service.py:195).
            similarity = max(-1.0, min(1.0, 1.0 - float(distance if distance is not None else 1.0)))
            parsed.append(
                {
                    "session_id": _field(entity, "session_id"),
                    "customer_id": _field(entity, "customer_id"),
                    "employee_id": _field(entity, "employee_id"),
                    "location_id": _field(entity, "location_id"),
                    "content": _field(entity, "content"),
                    "outcome": _field(entity, "outcome"),
                    "created_at": _field(entity, "created_at"),
                    "score": similarity,
                }
            )
        return parsed

    def has_session(self, session_id: str) -> bool:
        if self.count() == 0:
            return False
        rows = self.client.query(
            collection_name=COLLECTION,
            filter=f'session_id == "{session_id}"',
            output_fields=["memory_id"],
            limit=1,
        )
        return bool(rows)

    def count(self) -> int:
        try:
            stats = self.client.get_collection_stats(COLLECTION)
            return int(stats.get("row_count", 0))
        except Exception:
            return 0


def _field(entity: Any, name: str) -> Any:
    if isinstance(entity, dict):
        return entity.get(name)
    return getattr(entity, name, None)


def _build_filter(
    *,
    employee_id: str | None,
    customer_id: str | None,
    location_id: str | None = None,
    created_at_min: int | None = None,
    created_at_max: int | None = None,
) -> str | None:
    parts: list[str] = []
    if employee_id:
        parts.append(f'employee_id == "{employee_id}"')
    if customer_id:
        parts.append(f'customer_id == "{customer_id}"')
    if location_id:
        parts.append(f'location_id == "{location_id}"')
    if created_at_min is not None:
        parts.append(f"created_at >= {int(created_at_min)}")
    if created_at_max is not None:
        parts.append(f"created_at <= {int(created_at_max)}")
    return " and ".join(parts) or None


def _iso_to_epoch(iso: str) -> int | None:
    try:
        return int(time.mktime(time.strptime(iso, _ISO_FMT)))
    except (ValueError, TypeError):
        return None


_memory_service: SessionMemoryService | None = None


def get_memory_service() -> SessionMemoryService:
    """Lazy singleton so uvicorn reload can import routes without locking Milvus Lite."""
    global _memory_service
    if _memory_service is None:
        _memory_service = SessionMemoryService()
    return _memory_service
