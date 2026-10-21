"""Download progress: yt-dlp reports video and audio as separate streams; the overall
percent must average them and never run backwards."""

from app.checker import make_progress_hook, stream_percent


def test_stream_percent_from_bytes_and_fragments():
    assert stream_percent({"status": "downloading", "downloaded_bytes": 25, "total_bytes": 100}) == 25.0
    assert stream_percent({"status": "downloading", "fragment_index": 3, "fragment_count": 12}) == 25.0
    assert stream_percent({"status": "finished"}) == 100.0
    assert stream_percent({"status": "downloading"}) is None


def test_two_streams_are_averaged():
    seen = []
    hook = make_progress_hook(seen.append)
    info = {"requested_formats": [1, 2]}
    hook({"status": "downloading", "filename": "v", "downloaded_bytes": 50, "total_bytes": 100, "info_dict": info})
    assert seen[-1] == 25.0                     # video half done, audio not started
    hook({"status": "finished", "filename": "v", "info_dict": info})
    hook({"status": "downloading", "filename": "a", "downloaded_bytes": 100, "total_bytes": 100, "info_dict": info})
    assert seen[-1] == 100.0


def test_unknown_size_reports_none():
    seen = []
    make_progress_hook(seen.append)({"status": "downloading"})
    assert seen == [None]
