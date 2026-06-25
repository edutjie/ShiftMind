"""Test live session audio streaming and transcript."""
import asyncio
import json
import os

import pytest
import websockets


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_SESSION_TEST") != "1",
    reason="requires running local server with an active glasses session",
)


async def test_session_audio_stream():
    """Simulate glasses sending audio to an active session."""

    # First, check if there's an active session
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get("http://localhost:8000/api/session-status") as resp:
            data = await resp.json()
            active = data.get("active_session")
            if not active:
                print("❌ No active session. Start a session on the glasses first.")
                return

            session_id = active["session_id"]
            print(f"✓ Found active session: {session_id}")
            print(f"  Customer: {active['name']} (ID: {active['customer_id']})")
            print(f"  New customer: {active['is_new_customer']}")

    # Connect to the WebSocket
    ws_url = f"ws://localhost:8000/api/session/{session_id}/audio-stream"
    print(f"\n→ Connecting to {ws_url}")

    async with websockets.connect(ws_url) as ws:
        print("✓ WebSocket connected")

        # Wait for ASR status
        msg = await ws.recv()
        status = json.loads(msg)
        print(f"  ASR status: {status}")

        if status.get("type") == "asr_status":
            if "missing_doubao_env" in status.get("status", ""):
                print("\n❌ Doubao not configured. Set DOUBAO_ASR_API_KEY in .env")
                return
            elif "connected" in status.get("status", ""):
                print("  ✓ Doubao ASR connected and ready")

        # Send test audio (1 second of low-amplitude sine wave to simulate speech)
        import math
        sample_rate = 16000
        duration = 2  # 2 seconds
        num_samples = sample_rate * duration

        print(f"\n→ Sending {duration}s of test audio (sine wave)...")

        # Generate audio in chunks
        chunk_size = 3200  # 0.1 second chunks
        for i in range(0, num_samples, chunk_size):
            chunk = bytearray()
            for j in range(chunk_size):
                sample_index = i + j
                if sample_index >= num_samples:
                    break
                # Generate a 440 Hz sine wave (musical note A)
                value = int(5000 * math.sin(2 * math.pi * 440 * sample_index / sample_rate))
                # Convert to 16-bit PCM little-endian
                chunk.extend(value.to_bytes(2, byteorder='little', signed=True))

            await ws.send(chunk)
            await asyncio.sleep(0.1)  # Real-time simulation
            print(f"  Sent chunk {i//chunk_size + 1}/{(num_samples + chunk_size - 1)//chunk_size}")

        print("\n→ Sending EOF...")
        await ws.send("eof")

        # Wait for transcription results
        print("\n→ Waiting for transcription results...")
        timeout = 10
        start = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start < timeout:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(msg)
                if data.get("type") == "transcript":
                    final = data.get("final", False)
                    text = data.get("text", "")
                    final_text = data.get("final_text", "")
                    partial_text = data.get("partial_text", "")

                    print(f"  {'[FINAL]' if final else '[PARTIAL]'} text={repr(text)}")
                    print(f"    final_text={repr(final_text)}")
                    print(f"    partial_text={repr(partial_text)}")

                    if final:
                        print("\n✓ Received final transcript")
                        break
                elif data.get("type") == "asr_status":
                    print(f"  ASR status update: {data.get('status')}")
            except asyncio.TimeoutError:
                print("  (waiting...)")
                continue
            except websockets.exceptions.ConnectionClosed:
                print("\n  WebSocket closed by server")
                break

        print("\n✓ Test completed")


if __name__ == "__main__":
    try:
        asyncio.run(test_session_audio_stream())
    except KeyboardInterrupt:
        print("\n\nTest interrupted")
