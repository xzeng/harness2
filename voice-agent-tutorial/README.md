# Build a voice agent from scratch

One Python file, four stages, ~150 lines. The agent listens on your microphone,
transcribes with ElevenLabs Scribe, reasons with Claude (and can call a tool),
speaks with ElevenLabs TTS, and loops.

```
 mic ──► 1. record ──► 2. transcribe ──► 3. think (LLM + tools) ──► 4. speak ──► speaker
            (sounddevice)   (ElevenLabs STT)   (Claude, manual loop)   (ElevenLabs TTS)
                                   ▲                                        │
                                   └──────────── conversation history ◄─────┘
```

Every voice agent, including the phone-based ElevenLabs Agents product, is this
loop with better plumbing. Build it by hand once and the hosted products stop
being magic.

## 0. Prerequisites

- Python 3.10+
- A microphone and speaker
- PortAudio (`brew install portaudio` on macOS, `apt install libportaudio2` on Debian/Ubuntu)
- `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY` in your environment

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python agent.py
```

Press Enter, talk, press Enter. Say "goodbye" to exit. Completed requests land
in `requests.jsonl`.

## 1. Record (`record_push_to_talk`)

```python
with sd.InputStream(samplerate=16_000, channels=1, dtype="int16",
                    callback=lambda indata, *_: frames.put(indata.copy())):
    input("[Enter] to stop.")
```

`sounddevice` opens the mic and calls your callback with small int16 arrays.
We just queue them, then concatenate and wrap in a WAV header.

Why push-to-talk: it removes the hardest problem in voice (knowing when the
user stopped talking) so you can learn the rest first. Production systems
replace this with voice-activity detection (VAD) and barge-in. See step 7.

Why 16 kHz mono int16: it is the native format of most speech models and the
cheapest to move around. Using one format end to end avoids resampling.

## 2. Transcribe (`transcribe`)

```python
result = eleven.speech_to_text.convert(model_id="scribe_v2",
                                        file=("audio.wav", wav_bytes, "audio/wav"))
return result.text
```

One HTTP call, WAV in, text out. The `file` argument takes a
`(filename, bytes, mime)` tuple. `scribe_v2` is the batch model; there is a
`scribe_v2_realtime` WebSocket variant for streaming later.

## 3. Think (`think`)

This is the only part that makes it an *agent* rather than a chatbot.

```python
while True:
    resp = llm.beta.messages.create(model=..., system=SYSTEM_PROMPT,
                                    tools=TOOLS, messages=history, ...)
    history.append({"role": "assistant", "content": resp.content})
    if resp.stop_reason != "tool_use":
        return text_of(resp)
    # run each tool_use block, append ALL results as one user message
    history.append({"role": "user", "content": results})
```

The API is stateless, so `history` is the agent's memory and you send it every
turn. When Claude wants to act it returns `stop_reason == "tool_use"` plus
`tool_use` blocks. You execute them, append the results, and call again. The
loop exits when Claude answers in text.

Rules the code follows, each of which bites if skipped:

- Append `resp.content` whole, not just the text. The `tool_use` blocks must
  stay in history or the next call is rejected.
- Return every tool result in a single user message, matched by
  `tool_use_id`. Splitting them trains the model to stop parallelizing.
- A failing tool returns `is_error: True`, it does not raise. The model can
  recover; a crash cannot.
- Check `stop_reason` before reading content. `refusal` is a real outcome on
  current models, so `fallbacks="default"` is set to retry on a fallback model.

The system prompt is tuned for speech: short sentences, one question at a
time, no markdown. Text that looks fine on screen sounds terrible read aloud.

`output_config={"effort": "low"}` keeps latency down. Voice chat is the one
place where a faster, shallower answer beats a slower, deeper one.

### The tool

```python
TOOLS = [{"name": "log_record_request", "input_schema": {...}, "strict": True}]
TOOL_FUNCS = {"log_record_request": log_record_request}
```

A tool is a JSON schema plus a Python function with the same name. `strict:
True` guarantees the arguments validate against the schema, so
`fn(**block.input)` is safe. Here it writes a line to a file and returns a
confirmation id. In the county-recorder service this becomes
`record_outcome` writing to Postgres.

## 4. Speak (`speak`)

```python
pcm = eleven.text_to_speech.convert(voice_id=..., text=text,
                                    model_id="eleven_flash_v2_5", output_format="pcm_16000")
sd.play(np.frombuffer(b"".join(pcm), dtype="int16"), samplerate=16_000); sd.wait()
```

Requesting raw PCM instead of MP3 means no decoder dependency: the bytes go
straight to the sound card. `eleven_flash_v2_5` is the low-latency model.
`convert` returns a generator of chunks; joining them is the simplest correct
thing, streaming them is step 7.

## 5. The main loop

```python
speak(greeting)
while True:
    text = transcribe(record_push_to_talk())
    history.append({"role": "user", "content": text})
    reply = think(history)
    speak(reply)
```

Serial and blocking on purpose. Each stage is a pure function with an obvious
input and output, which is what you want when debugging audio.

## 6. A sample run

```
Agent: Hi, this is the records assistant. Which county and what record do you need?
You:   Travis County, I need a deed.
Agent: Sure. What is the parcel or document number?
You:   Document number 2024 dash 0113.
Agent: Your request is logged. The confirmation id is R E C dash 4 F 9 A 2 C.
You:   Thanks, goodbye.
Agent: Goodbye.
```

## 7. From here to production

In order of payoff:

1. **Stream TTS.** Play chunks as they arrive instead of after `b"".join`.
   Cuts perceived latency roughly in half.
2. **Sentence-split the LLM output.** Stream Claude's text, send each finished
   sentence to TTS immediately. The agent starts talking before it finishes
   thinking.
3. **Replace push-to-talk with VAD.** Use `scribe_v2_realtime` over a
   WebSocket and let end-of-speech detection end the turn.
4. **Barge-in.** Stop playback when VAD detects the user speaking.
5. **Put it on a phone.** Audio comes from Twilio Media Streams or a SIP trunk
   as 8 kHz mu-law instead of your mic. Nothing in stages 2 to 4 changes.

Steps 1 to 5 are exactly what ElevenLabs Agents does for you when you call
`/v1/convai/twilio/outbound-call`. The system prompt, the tools, and the tool
functions from this file carry over unchanged as the agent prompt, the server
tools, and your tool-webhook handlers.
