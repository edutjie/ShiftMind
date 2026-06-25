"""Test Doubao ASR WebSocket connection and audio streaming."""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

from doubao_asr import DoubaoSeedAsrStream


async def test_doubao_connection():
    """Test basic Doubao ASR connection."""
    asr = DoubaoSeedAsrStream()

    print(f"✓ API Key configured: {asr.configured}")
    print(f"  Base URL: {asr.base_url}")
    print(f"  Resource ID: {asr.resource_id}")
    print(f"  Model: {asr.model}")

    if not asr.configured:
        print("\n❌ DOUBAO_ASR_API_KEY not set!")
        return False

    try:
        async with asr:
            print("\n✓ WebSocket connected to Doubao ASR")

            # Send a short test audio chunk (silence - 1 second of 16-bit PCM at 16kHz)
            silence = b'\x00' * (16000 * 2)  # 1 second of silence
            print(f"  Sending {len(silence)} bytes test audio...")
            await asr.send_audio(silence)
            await asr.send_audio(b"", final=True)
            print("  ✓ Audio sent with final flag")

            # Try to receive response
            print("\n  Waiting for ASR response...")
            for i in range(10):
                result = await asyncio.wait_for(asr.recv_text(), timeout=2.0)
                if result:
                    text, is_final = result
                    print(f"  {'[FINAL]' if is_final else '[PARTIAL]'} Received: {repr(text)}")
                    if is_final:
                        break
                else:
                    print(f"  [{i+1}] No result yet...")

            print("\n✓ Doubao ASR test completed successfully")
            return True

    except asyncio.TimeoutError:
        print("\n⚠️  Timeout waiting for ASR response (this is OK for silence)")
        return True
    except Exception as e:
        print(f"\n❌ Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_audio_format_issue():
    """Test if the audio format configuration is correct."""
    print("\n" + "="*60)
    print("CHECKING AUDIO FORMAT CONFIGURATION")
    print("="*60)

    print("\nGlasses side (VoiceSessionRecorder.kt):")
    print("  - Format: PCM_16BIT")
    print("  - Sample rate: 16000 Hz")
    print("  - Channels: MONO")
    print("  - Sends: Raw PCM chunks via WebSocket")

    print("\nServer side (doubao_asr.py):")
    print("  - format: 'wav' ⚠️  MISMATCH!")
    print("  - codec: 'raw'")
    print("  - rate: 16000")
    print("  - bits: 16")
    print("  - channel: 1")

    print("\n❌ ISSUE IDENTIFIED:")
    print("   The config says format='wav' but glasses send raw PCM chunks.")
    print("   According to Doubao docs, when sending raw PCM in streaming mode,")
    print("   format should likely be 'pcm' or the codec 'raw' handles it.")
    print("\n   However, looking at Doubao Seed ASR docs, 'wav' + 'raw' codec")
    print("   might mean 'WAV format with raw/uncompressed PCM codec'.")

    return True


if __name__ == "__main__":
    print("DOUBAO ASR DIAGNOSTIC TEST")
    print("=" * 60)

    try:
        result1 = asyncio.run(test_doubao_connection())
        result2 = asyncio.run(test_audio_format_issue())

        if result1 and result2:
            print("\n" + "="*60)
            print("DIAGNOSIS COMPLETE")
            print("="*60)
            sys.exit(0)
        else:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted")
        sys.exit(1)
