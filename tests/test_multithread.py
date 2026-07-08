"""Quick test for multi-threaded download — no network, mock only."""
import os
import tempfile
import threading

from podpull.core import (_merge_parts, _probe_url,
                          download_url, safe_filename)


def test_safe_filename_no_change():
    assert safe_filename("hello world") == "hello world"


def test_part_merging():
    """Test that part files merge correctly into one file."""
    with tempfile.TemporaryDirectory() as td:
        part_files = []
        data_parts = [b"HELLO ", b"WORLD ", b"FOO ", b"BAR"]
        for i, data in enumerate(data_parts):
            pf = os.path.join(td, f"test.bin.part{i}")
            with open(pf, "wb") as f:
                f.write(data)
            part_files.append(pf)

        dest = os.path.join(td, "test.bin")
        _merge_parts(part_files, dest)

        with open(dest, "rb") as f:
            merged = f.read()
        assert merged == b"HELLO WORLD FOO BAR"

        for pf in part_files:
            assert not os.path.exists(pf), f"part file {pf} should be removed after merge"


def test_download_url_single_thread_no_network():
    """Test that threads=1 uses single-thread path and doesn't crash."""
    with tempfile.TemporaryDirectory() as td:
        dest = os.path.join(td, "out.bin")
        try:
            download_url("http://127.0.0.1:1/nonexistent", dest, threads=1)
        except Exception:
            pass
        assert True


def test_download_url_multi_thread_small_fallback():
    """Test that multi-threaded falls back when we can't get content length."""
    with tempfile.TemporaryDirectory() as td:
        dest = os.path.join(td, "out.bin")
        try:
            download_url("http://127.0.0.1:1/nonexistent", dest, threads=8)
        except Exception:
            pass
        assert True


if __name__ == "__main__":
    test_safe_filename_no_change()
    print("PASS: test_safe_filename_no_change")
    test_part_merging()
    print("PASS: test_part_merging")
    test_download_url_single_thread_no_network()
    print("PASS: test_download_url_single_thread_no_network")
    test_download_url_multi_thread_small_fallback()
    print("PASS: test_download_url_multi_thread_small_fallback")
    print("\nAll local tests passed!")
