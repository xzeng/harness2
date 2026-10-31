"""
Minimal voice agent: microphone -> speech-to-text -> LLM (with one tool) -> text-to-speech -> speaker.

Run:  ANTHROPIC_API_KEY=... ELEVENLABS_API_KEY=... python agent.py
Quit: say "goodbye" or press Ctrl-C.
"""
import io
import json
import os
import queue
import sys
import uuid
import wave

import anthropic
import numpy as np
import sounddevice as sd
from elevenlabs.client import ElevenLabs

# ---------------------------------------------------------------- config
SAMPLE_RATE = 16_000                  # 16 kHz mono int16 everywhere: mic, STT, TTS
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # "Rachel"
STT_MODEL = "scribe_v2"
TTS_MODEL = "eleven_flash_v2_5"       # lowest-latency TTS model
LLM_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are a phone assistant that helps people request public records
from a county recorder's office. You are speaking out loud, so:
- Reply in one or two short sentences. No lists, no markdown.
- Ask for one missing detail at a time.
- When you have the county, record type, and parcel or document number,
  call log_record_request, then read the confirmation id back to the caller.
- If the caller says goodbye, say goodbye and nothing else."""

TOOLS = [{
    "name": "log_record_request",
    "description": "Save a completed public-records request and return a confirmation id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "county": {"type": "string"},
            "record_type": {"type": "string", "description": "e.g. deed, lien, plat map"},
            "identifier": {"type": "string", "description": "parcel number or document number"},
        },
        "required": ["county", "record_type", "identifier"],
        "additionalProperties": False,
    },
    "strict": True,
}]

llm = anthropic.Anthropic()           # reads ANTHROPIC_API_KEY
eleven = ElevenLabs()                 # reads ELEVENLABS_API_KEY


# ---------------------------------------------------------------- 1. listen
def record_push_to_talk() -> bytes:
    """Press Enter to start, Enter again to stop. Returns WAV bytes."""
    frames: "queue.Queue[np.ndarray]" = queue.Queue()
    input("\n[Enter] to start talking...")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        callback=lambda indata, *_: frames.put(indata.copy())):
        input("[Enter] to stop.")
    pcm = np.concatenate(list(frames.queue)) if not frames.empty() else np.zeros((0, 1), "int16")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- 2. transcribe
def transcribe(wav_bytes: bytes) -> str:
    result = eleven.speech_to_text.convert(model_id=STT_MODEL, file=("audio.wav", wav_bytes, "audio/wav"))
    return (result.text or "").strip()


# ---------------------------------------------------------------- 3. think (+ tools)
def log_record_request(county: str, record_type: str, identifier: str) -> str:
    confirmation = f"REC-{uuid.uuid4().hex[:6].upper()}"
    with open("requests.jsonl", "a") as f:
        f.write(json.dumps({"id": confirmation, "county": county,
                            "record_type": record_type, "identifier": identifier}) + "\n")
    return json.dumps({"confirmation_id": confirmation})


TOOL_FUNCS = {"log_record_request": log_record_request}


def think(history: list) -> str:
    """Run the agent loop until Claude produces a spoken reply. Mutates history."""
    while True:
        resp = llm.beta.messages.create(
            model=LLM_MODEL,
            max_tokens=1024,                       # spoken replies are short by design
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
            output_config={"effort": "low"},       # chat latency matters more than depth
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                   # if a safety classifier declines, retry on a fallback model
        )
        history.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "refusal":
            return "Sorry, I can't help with that."
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")

        # Execute every tool call, return all results in ONE user message.
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                fn = TOOL_FUNCS[block.name]
                try:
                    out = fn(**block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                except Exception as e:  # report tool failures to the model, don't crash
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(e), "is_error": True})
        history.append({"role": "user", "content": results})


# ---------------------------------------------------------------- 4. speak
def speak(text: str) -> None:
    pcm_chunks = eleven.text_to_speech.convert(
        voice_id=VOICE_ID, text=text, model_id=TTS_MODEL, output_format="pcm_16000")
    audio = np.frombuffer(b"".join(pcm_chunks), dtype="int16")
    sd.play(audio, samplerate=SAMPLE_RATE)
    sd.wait()


# ---------------------------------------------------------------- main loop
def main() -> None:
    history: list = []
    speak("Hi, this is the records assistant. Which county and what record do you need?")
    while True:
        text = transcribe(record_push_to_talk())
        if not text:
            print("(heard nothing)"); continue
        print(f"You:   {text}")
        history.append({"role": "user", "content": text})
        reply = think(history)
        print(f"Agent: {reply}")
        speak(reply)
        if "goodbye" in text.lower():
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
