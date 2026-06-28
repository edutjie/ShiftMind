"""Customer interaction session state and LLM summarization."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from auth import active_login
from face_recognition_service import MODEL_NAME, RecognitionResult, get_face_service
from storage import storage


@dataclass(frozen=True)
class ActiveSession:
    session_id: str
    customer_id: str
    name: str
    is_new_customer: bool
    memory_card: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_status": "active",
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "name": self.name,
            "is_new_customer": self.is_new_customer,
            "memory_card": self.memory_card,
        }


class SessionService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: ActiveSession | None = None
        self._live: dict[str, dict[str, Any]] = {}

    def active(self) -> ActiveSession | None:
        with self._lock:
            if self._active:
                return self._active
            row = storage.latest_active_session()
            if not row:
                return None
            self._active = _active_from_row(row)
            return self._active

    def ensure_from_recognition(self, result: RecognitionResult, crop_jpeg: bytes | None) -> ActiveSession | None:
        if result.status not in {"confirmed", "unrecognized_first_visit"}:
            return None
        with self._lock:
            if self._active:
                return self._active
            row = storage.latest_active_session()
            if row:
                self._active = _active_from_row(row)
                return self._active

            login = active_login.read()
            if result.status == "confirmed" and result.person_id:
                customer = storage.get_customer(result.person_id)
                if not customer:
                    return None
                session = storage.create_customer_session(
                    customer_id=customer.id,
                    is_new_customer=False,
                    employee_id=login.employee_id if login else None,
                    location_id=login.location_id if login else None,
                )
                self._active = ActiveSession(
                    session_id=session["id"],
                    customer_id=customer.id,
                    name=customer.name,
                    is_new_customer=False,
                    memory_card=customer.memory_card,
                )
                return self._active

            if not crop_jpeg:
                return None
            customer_id = storage.next_customer_id()
            name = "Unknown customer"
            memory_card = "unrecognized, first visit"
            get_face_service().enroll_latest_crop(
                customer_id=customer_id,
                name=name,
                memory_card=memory_card,
                crop_jpeg=crop_jpeg,
            )
            session = storage.create_customer_session(
                customer_id=customer_id,
                is_new_customer=True,
                employee_id=login.employee_id if login else None,
                location_id=login.location_id if login else None,
            )
            self._active = ActiveSession(
                session_id=session["id"],
                customer_id=customer_id,
                name=name,
                is_new_customer=True,
                memory_card=memory_card,
            )
            return self._active

    def finish(self, *, session_id: str, audio_bytes: bytes | None, transcript_hint: str = "") -> dict[str, Any]:
        with self._lock:
            active = self._active
            if active and active.session_id != session_id:
                raise ValueError("not_active_session")
        row = storage.get_customer_session(session_id)
        if not row:
            raise KeyError(session_id)

        transcript = transcript_hint.strip()
        llm_status = "not_configured"
        if audio_bytes and not transcript:
            transcript, llm_status = _transcribe_audio(audio_bytes)
        summary_data = _summarize_session(
            customer_id=str(row["customer_id"]),
            session_id=session_id,
            existing_name=str(row.get("customer_name") or ""),
            existing_memory=str(row.get("memory_card") or ""),
            is_new_customer=bool(row.get("is_new_customer")),
            transcript=transcript,
        )
        if summary_data.get("_llm_status"):
            llm_status = str(summary_data.pop("_llm_status"))

        summary = str(summary_data.get("summary") or "").strip() or "Session ended. No transcript summary available yet."
        points = [str(p).strip() for p in summary_data.get("important_points", []) if str(p).strip()]
        extracted_name = str(summary_data.get("name") or "").strip()
        topics = [str(t).strip() for t in summary_data.get("topics", []) if str(t).strip()]
        outcome = str(summary_data.get("outcome") or "unknown").strip() or "unknown"

        finished = storage.finish_customer_session(
            session_id=session_id,
            transcript=transcript,
            summary=summary,
            important_points=points,
            audio_bytes=audio_bytes,
            topics=topics,
            outcome=outcome,
        )

        # Best-effort semantic-index upsert for the Q&A agent. Never let a
        # Milvus/embedding failure break session finish — the storage UPDATE
        # above has already committed.
        try:
            content = (summary + "\n" + "; ".join(points)).strip()
            if content and not content.startswith("Session ended."):
                from session_memory_service import get_memory_service

                get_memory_service().upsert_memory(
                    memory_id=f"mem-{session_id}",
                    session_id=session_id,
                    customer_id=str(row["customer_id"]),
                    employee_id=(row.get("employee_id") or None),
                    location_id=(row.get("location_id") or None),
                    content=content,
                    outcome=outcome,
                    started_at_iso=str(row.get("started_at") or ""),
                )
        except Exception as exc:
            print(f"[finish] session_memories upsert failed: {type(exc).__name__}: {exc}")

        customer = storage.get_customer(str(row["customer_id"]))
        if customer:
            new_name = customer.name
            if bool(row.get("is_new_customer")) and extracted_name:
                new_name = extracted_name

            # Get historical session data for better context
            session_history = storage.get_customer_session_history(customer.id, limit=5)

            # Compose cumulative memory card using LLM with full historical context
            memory_card = _update_memory_card_with_llm(
                customer_id=customer.id,
                existing_memory=customer.memory_card,
                new_summary=summary,
                new_points=points,
                session_history=session_history,
            )

            storage.upsert_customer(
                customer_id=customer.id,
                name=new_name,
                memory_card=memory_card,
                preferences=points,
            )

        with self._lock:
            if self._active and self._active.session_id == session_id:
                self._active = None

        return {
            "ok": True,
            "session_status": "ended",
            "session_id": session_id,
            "customer_id": row["customer_id"],
            "transcript": transcript,
            "summary": summary,
            "important_points": points,
            "extracted_name": extracted_name or None,
            "llm_status": llm_status,
            "stored": finished,
        }

    def append_live_transcript(self, session_id: str, text: str, *, final: bool) -> dict[str, Any]:
        text = text.strip()
        if not text:
            return self.live_transcript(session_id)
        with self._lock:
            state = self._live.setdefault(
                session_id,
                {"final_text": "", "partial_text": "", "updated_at": 0.0},
            )
            if final:
                prev = str(state.get("final_text") or "").strip()
                if text not in prev[-max(len(text), 1):]:
                    state["final_text"] = (prev + " " + text).strip()
                state["partial_text"] = ""
            else:
                state["partial_text"] = text
            state["updated_at"] = time.monotonic()
            return dict(state)

    def live_transcript(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._live.get(session_id) or {"final_text": "", "partial_text": "", "updated_at": 0.0})

    def reset(self) -> None:
        with self._lock:
            self._active = None


def _active_from_row(row: dict[str, Any]) -> ActiveSession:
    return ActiveSession(
        session_id=str(row["id"]),
        customer_id=str(row["customer_id"]),
        name=str(row.get("customer_name") or row["customer_id"]),
        is_new_customer=bool(row.get("is_new_customer")),
        memory_card=str(row.get("memory_card") or ""),
    )


def _openai_client() -> Any | None:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("SHIFTMIND_LLM_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None
    kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("SHIFTMIND_LLM_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _transcribe_audio(audio_bytes: bytes) -> tuple[str, str]:
    client = _openai_client()
    if client is None:
        return "", "missing_openai_config"
    import io

    model = os.getenv("SHIFTMIND_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    audio = io.BytesIO(audio_bytes)
    audio.name = "session.wav"
    try:
        result = client.audio.transcriptions.create(model=model, file=audio)
        return str(getattr(result, "text", "") or ""), "transcribed"
    except Exception as exc:
        return "", f"transcribe_failed:{type(exc).__name__}"


def _summarize_session(
    *,
    customer_id: str,
    session_id: str | None = None,
    existing_name: str,
    existing_memory: str,
    is_new_customer: bool,
    transcript: str,
) -> dict[str, Any]:
    if not transcript.strip():
        return {
            "summary": "Session ended. No transcript captured.",
            "important_points": [],
            "name": "" if not is_new_customer else "",
            "topics": [],
            "outcome": "unknown",
            "_llm_status": "no_transcript",
        }
    client = _openai_client()
    if client is None:
        return {
            "summary": transcript[:500],
            "important_points": [],
            "name": "" if not is_new_customer else "",
            "topics": [],
            "outcome": "unknown",
            "_llm_status": "missing_openai_config",
        }
    model = os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")

    # Build system message with context about existing customer data
    context = f"Customer ID: {customer_id}"
    if existing_name:
        context += f"\nKnown name: {existing_name}"
    if existing_memory:
        context += f"\nExisting memory card: {existing_memory}"
    last = storage.get_last_interaction_by_customer(customer_id, exclude_session_id=session_id)
    if last and last.get("employee_name"):
        context += f"\nLast employee who interacted with this customer: {last['employee_name']}"
        if last.get("location_name"):
            context += f" at {last['location_name']}"
        if last.get("ended_at"):
            context += f" on {last['ended_at']}"

    system_prompt = (
        "You are a retail assistant that extracts key customer information from interaction transcripts. "
        f"{context}\n\n"
        "Extract and return ONLY valid JSON with these keys:\n"
        "- summary: A brief 1-2 sentence summary of this interaction\n"
        "- important_points: Array of durable facts/preferences (likes, dislikes, needs, personal info)\n"
        "- name: Customer's name if mentioned in transcript (empty string otherwise)\n"
        "- topics: Array of 1-5 short lowercase tags describing what the conversation was about "
        "(e.g. [\"returns\",\"warranty\",\"refund\"], [\"football boots\",\"sizing\"])\n"
        "- outcome: One of \"converted\" (customer committed to a purchase/booking), "
        "\"not_converted\" (left without committing), or \"unknown\" (can't tell)\n\n"
        "Focus on NEW information from this transcript. If there's existing memory, ADD to it, don't repeat it."
    )

    user_prompt = f"New customer: {is_new_customer}\n\nTranscript:\n{transcript}\n\nReturn JSON only, no markdown."

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        text = resp.choices[0].message.content or ""
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            # Coerce topics/outcome so downstream code can rely on the keys
            # existing even if the LLM omitted them.
            if not isinstance(parsed.get("topics"), list):
                parsed["topics"] = []
            parsed["topics"] = [str(t).strip().lower() for t in parsed["topics"] if str(t).strip()]
            outcome = str(parsed.get("outcome") or "unknown").strip().lower()
            parsed["outcome"] = outcome if outcome in {"converted", "not_converted", "unknown"} else "unknown"
            parsed["_llm_status"] = "summarized"
            return parsed
    except Exception as exc:
        return {
            "summary": transcript[:500],
            "important_points": [],
            "name": "",
            "topics": [],
            "outcome": "unknown",
            "_llm_status": f"summarize_failed:{type(exc).__name__}",
        }
    return {
        "summary": transcript[:500],
        "important_points": [],
        "name": "",
        "topics": [],
        "outcome": "unknown",
        "_llm_status": "summarize_parse_failed",
    }


def _update_memory_card_with_llm(
    customer_id: str,
    existing_memory: str,
    new_summary: str,
    new_points: list[str],
    session_history: list[dict[str, Any]] | None = None,
) -> str:
    """
    Use LLM to maintain a concise, cumulative memory card for AR glasses display.

    The memory card must be brief (2-3 lines max) but capture all important facts
    across multiple sessions. LLM re-summarizes using full session history context.

    Args:
        customer_id: Customer identifier
        existing_memory: Current memory card text
        new_summary: Summary from the just-completed session
        new_points: Important points extracted from the just-completed session
        session_history: List of recent session data (summaries + important_points)
    """
    # Skip if new session has no meaningful content
    if not new_summary or new_summary.startswith("Session ended."):
        return existing_memory

    # For first session, just use the summary + points
    if not existing_memory or existing_memory.startswith("Session ended."):
        if new_points:
            return new_summary.strip() + " • " + "; ".join(new_points[:5])
        return new_summary.strip()

    # Use LLM to create a concise, cumulative memory card
    client = _openai_client()
    if client is None:
        # Fallback: simple concatenation
        return _compose_memory_card(new_summary, new_points, existing_memory)

    model = os.getenv("SHIFTMIND_LLM_MODEL", "gpt-4o-mini")

    # Build context from session history
    history_context = ""
    if session_history:
        history_lines = []
        for i, session in enumerate(session_history[:3], 1):  # Last 3 sessions max
            summary = session.get("summary", "")
            points_json = session.get("important_points_json", "[]")
            try:
                points = json.loads(points_json) if points_json else []
            except:
                points = []

            if summary:
                emp = session.get("employee_name")
                loc = session.get("location_name")
                who = ""
                if emp:
                    who = f" (with {emp}" + (f", {loc}" if loc else "") + ")"
                history_lines.append(f"Session {i}: {summary}{who}")
                if points:
                    history_lines.append(f"  Facts: {', '.join(points[:3])}")

        if history_lines:
            history_context = "\n\nPREVIOUS SESSIONS:\n" + "\n".join(history_lines)

    system_prompt = (
        "You maintain concise customer memory cards for AR smart glasses display.\n\n"
        "CRITICAL CONSTRAINTS:\n"
        "- Maximum 2-3 short sentences (under 150 characters total)\n"
        "- Focus on ACTIONABLE facts: preferences, needs, personal details\n"
        "- Merge redundant information across sessions\n"
        "- Keep most recent/important facts, drop outdated ones\n"
        "- Use bullet points for multiple facts\n\n"
        "This will be displayed on a small AR heads-up display, so brevity is critical."
    )

    user_prompt = (
        f"Customer ID: {customer_id}\n\n"
        f"CURRENT MEMORY CARD:\n{existing_memory}\n"
        f"{history_context}\n\n"
        f"NEW SESSION:\n{new_summary}\n"
        f"New Facts: {', '.join(new_points) if new_points else 'None'}\n\n"
        "Synthesize ALL knowledge into ONE ultra-concise memory card (max 150 chars). "
        "Prioritize recent facts but keep enduring preferences. Remove redundancy:"
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=100,
        )

        updated_memory = (resp.choices[0].message.content or "").strip()

        # Validate length - if LLM produced too much, truncate
        if len(updated_memory) > 200:
            updated_memory = updated_memory[:197] + "..."

        return updated_memory if updated_memory else existing_memory

    except Exception as exc:
        # Fallback: use simple composition
        return _compose_memory_card(new_summary, new_points, existing_memory)


def _compose_memory_card(summary: str, points: list[str], existing_memory: str = "") -> str:
    """
    Fallback memory card composition without LLM (simple concatenation).
    Used when LLM is unavailable.
    """
    new_parts = []

    # Add new summary if it's meaningful (not just a fallback message)
    if summary and not summary.startswith("Session ended."):
        new_parts.append(summary.strip()[:100])  # Truncate long summaries

    # Add important points
    if points:
        new_parts.append("• " + "; ".join(points[:3]))  # Max 3 points

    # Combine with existing memory
    if existing_memory and not existing_memory.startswith("Session ended."):
        # If there's both existing and new, separate with newline
        if new_parts:
            combined = existing_memory.strip() + "\n" + " ".join(new_parts)
            # Truncate if too long
            if len(combined) > 200:
                return combined[:197] + "..."
            return combined
        return existing_memory.strip()

    # Just new memory
    result = " ".join(new_parts) if new_parts else summary.strip()
    if len(result) > 200:
        return result[:197] + "..."
    return result


session_service = SessionService()
