from pydub import AudioSegment

# Load your audio files
voice = AudioSegment.from_mp3("voiceOutput.mp3")
background = AudioSegment.from_mp3("musicOutput.mp3")
background = AudioSegment.from_mp3("musicOutput.mp3")

# Adjust background music volume (usually lower so it doesn't overpower the voice)
background = background - 5  # reduce volume by 15 dB

# If background is shorter than the voice, loop it
if len(background) < len(voice):
    background = background * (len(voice) // len(background) + 1)

# Trim background to the length of the voice
background = background[:len(voice)]

# Overlay the two tracks
combined = voice.overlay(background)

# Export the final audio
combined.export("finalOutput.mp3", format="mp3")
