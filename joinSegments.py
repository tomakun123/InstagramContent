from pydub import AudioSegment

file1_path = "Part1.mp3"
file2_path = "Part2.mp3"


# Load your audio files
def concatenate_audio(file1_path, file2_path):
    audio1 = AudioSegment.from_file(file1_path)
    audio2 = AudioSegment.from_file(file2_path)

    # Concatenate the audio files
    combined = audio1 + audio2

    # Export the combined audio
    combined.export("voiceOutput.mp3", format="mp3")  # or use "wav", etc.


# Example usage
if __name__ == "__main__":
    concatenate_audio(file1_path, file2_path)
