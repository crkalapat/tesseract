from gpiozero import Button
import pyaudio
import wave
import requests
import json
import base64
import numpy as np

CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100

p = pyaudio.PyAudio()

vol_up_button = Button(12)
vol_down_button = Button(7)
mute_button = Button(1)
wake_button = Button(16)

volume = 0.3 # double from 0.0 to 1.0 for percentage of volume
is_muted = False

def encode_audio_to_base64(audio_path):
    with open(audio_path, "rb") as audio_file:
        return base64.b64encode(audio_file.read()).decode('utf-8')
    
def transcribe_audio(audio_path):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY_REF}",
        "Content-Type": "application/json"
    }

    # Read and encode the audio file
    base64_audio = encode_audio_to_base64(audio_path)

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Please transcribe this audio file."
                },
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": base64_audio,
                        "format": "wav"
                    }
                }
            ]
        }
    ]

    payload = {
        "model": "google/gemini-2.5-flash",
        "messages": messages
    }

    response = requests.post(url, headers=headers, json=payload)
    return response.json

def raise_volume():
    if volume < 0.9:
        volume += 0.1
    else:
        volume = 1.0

def lower_volume():
    if volume < 0.1:
        volume -= 0.1
    else:
        volume = 0.0

def toggle_mic():
    is_muted = not is_muted

def play_audio(audio_path):
    with wave.open(audio_path, 'rb') as wf:
        stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                        channels=wf.getnchannels(),
                        rate=wf.getframerate(),
                        output=True)
        
        data = wf.readframes(CHUNK)
        while len(data):
            volume_scaled_data = (np.frombuffer(data, dtype=np.int16) * volume).astype(np.int16).tobytes()
            stream.write(volume_scaled_data)
            data = wf.readframes(CHUNK)

        stream.close()

def record_audio():
    if not is_muted:
        with wave.open('input.wav', 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(p.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True)

            print('Recording...')

            while wake_button.is_pressed:
                wf.writeframes(stream.read(CHUNK))
            print('Done')

            stream.close()
        
        play_audio(get_llm_response('input.wav'))

def get_llm_response(audio_path):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY_REF}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "openai/gpt-4o-audio-preview",
        "messages": [
            {
                "role": "user",
                "content": transcribe_audio(audio_path)
            }
        ],
        "modalities": ["text", "audio"],
        "audio": {
            "voice": "alloy",
            "format": "wav"
        },
        "stream": True
    }

    # Audio output requires streaming — the response is delivered as SSE chunks
    response = requests.post(url, headers=headers, json=payload, stream=True)

    audio_data_chunks = []
    transcript_chunks = []

    for line in response.iter_lines():
        if not line:
            continue
        decoded = line.decode("utf-8")
        if not decoded.startswith("data: "):
            continue
        data = decoded[len("data: "):]
        if data.strip() == "[DONE]":
            break
        chunk = json.loads(data)
        delta = chunk["choices"][0].get("delta", {})
        audio = delta.get("audio", {})
        if audio.get("data"):
            audio_data_chunks.append(audio["data"])
        if audio.get("transcript"):
            transcript_chunks.append(audio["transcript"])

    transcript = "".join(transcript_chunks)
    print(f"Transcript: {transcript}")

    # Combine and decode the base64 audio chunks, then save
    full_audio_b64 = "".join(audio_data_chunks)
    audio_bytes = base64.b64decode(full_audio_b64)
    with open("output.wav", "wb") as f:
        f.write(audio_bytes)

vol_up_button.when_pressed = raise_volume
vol_down_button.when_pressed = lower_volume
mute_button.when_pressed = toggle_mic
wake_button.when_pressed = record_audio
