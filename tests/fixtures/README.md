# Synthetic audio

`tone.mp3` and `tone.flac` contain only a generated five-second 440 Hz sine wave. They contain no downloaded music. Generated locally with FFmpeg:

```
ffmpeg -f lavfi -i sine=frequency=440:duration=5 -ac 1 -ar 44100 -b:a 320k -map_metadata -1 tone.mp3
ffmpeg -f lavfi -i sine=frequency=440:duration=5 -ac 1 -ar 44100 -c:a flac -map_metadata -1 tone.flac
ffmpeg -f lavfi -i sine=frequency=440:duration=5 -ac 1 -ar 192000 -sample_fmt s32 -c:a flac -map_metadata -1 tone-hires.flac
```

These fixtures are dedicated to the public domain (CC0). Tests use real Mutagen parsing/tagging. Production validation checks format metadata, nonempty media frames, transport length, and MP3 duration when provided; it does not decode every FLAC frame or certify bit-perfect source audio.
