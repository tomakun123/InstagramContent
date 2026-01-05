from moviepy import VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip
from moviepy.video.tools.subtitles import SubtitlesClip
import random
import numpy as np

import whisper_timestamped as whisper

video_clip = VideoFileClip("MCParkour.mp4")  # for videos
video_clip = video_clip.without_audio()
# video file clips already have fps and duration
print("Clip duration: {}".format(video_clip.duration))
print("Clip fps: {}".format(video_clip.fps))

audio_clip = AudioFileClip("finalOutput.mp3")

# Select random segment of video
start_time = random.uniform(0, video_clip.duration - audio_clip.duration)
# New Video with New Duration
video_segment = video_clip.subclipped(start_time, start_time + audio_clip.duration)
print(f"Using video segment from {start_time:.2f}s to {start_time + audio_clip.duration:.2f}s")

print("Clip duration: {}".format(video_segment.duration))  # Cuting will update duration
print("Clip fps: {}".format(video_segment.fps))  # and keep fps

# And finally we can write the result into a file
# Here we just save as MP4, inheriting FPS, etc. from final_clip

# Get .srt file from video
def get_transcribed_text(filename):
    audio = whisper.load_audio(filename)
    model = whisper.load_model("small", device="cpu")
    results = whisper.transcribe(model, audio, language="en")

    return results["segments"]

def get_text_clips(text, max_chars_per_clip=20):
    text_clips = []

    for segment in text:
        words = segment["words"]
        current_text = ""
        current_start = words[0]["start"]
        current_end = words[0]["end"]

        for word in words:
            # Check if adding the next word exceeds the limit
            if len(current_text) + len(word["text"]) + 1 <= max_chars_per_clip:
                if current_text:
                    current_text += " "
                current_text += word["text"]
                current_end = word["end"]
            else:
                # Create the clip for the current group
                text_clips.append(
                    TextClip(
                        text=current_text,
                        method='caption',
                        font_size=30,
                        size=(1920, 1080),
                        stroke_width=5,
                        stroke_color="black",
                        font="font/use.ttf",
                        color="white"
                    )
                    .with_start(current_start)
                    .with_end(current_end)
                    .with_position("center")
                )
                # Start a new group
                current_text = word["text"]
                current_start = word["start"]
                current_end = word["end"]

        # Add the last clip if any text remains
        if current_text:
            text_clips.append(
                TextClip(
                    text=current_text,
                    method='caption',
                    font_size=30,
                    size=(1920, 1080),
                    stroke_width=5,
                    stroke_color="black",
                    font="font/use.ttf",
                    color="white"
                )
                .with_start(current_start)
                .with_end(current_end)
                .with_position("center")
            )

    return text_clips

# Loading the video as a VideoFileClip
transcribed_text = get_transcribed_text("finalOutput.mp3")
# Load the audio in the video to transcribe it and get transcribed text

# Generate text elements for video using transcribed text
text_clip_list = get_text_clips(text=transcribed_text)
# Create a CompositeVideoClip that we write to a file
final_clip = CompositeVideoClip([video_segment] + text_clip_list).with_audio(audio_clip)

# Write the final video once (video with subtitles + audio)
final_clip.write_videofile("final.mp4", codec="libx264")