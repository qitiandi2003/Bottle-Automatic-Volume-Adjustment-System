"""Independent local recording, with codecs, threads and HTTP all mocked."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


_ROOT = Path(__file__).resolve().parents[2]


class DebugStreamTests(unittest.TestCase):
    def setUp(self):
        # Load an isolated copy; these tests need neither numpy/OpenCV nor sockets.
        self.cv2 = mock.Mock(IMWRITE_JPEG_QUALITY=1)
        spec = importlib.util.spec_from_file_location("debug_stream_under_test", _ROOT / "debug_stream.py")
        self.stream = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"cv2": self.cv2, "numpy": mock.Mock()}):
            spec.loader.exec_module(self.stream)
        self.stream._ENABLED = False
        self.stream._MAX_WIDTH = 0

    def test_recording_with_http_disabled_encodes_and_saves_without_opening_server(self):
        slot = self.stream._Slot("overlay")
        received = []

        def record(name, data):
            received.append((name, data))
            slot.shutdown.set()

        self.stream.set_recorder_hook(record)
        self.cv2.imencode.return_value = (True, SimpleNamespace(tobytes=lambda: b"jpeg"))
        frame = object()
        with mock.patch.object(self.stream, "_QuietServer") as server, \
                mock.patch.object(self.stream.threading, "Thread") as thread, \
                mock.patch.object(self.stream, "_get_or_create_slot", return_value=slot):
            self.stream.start()
            self.stream.push("overlay", frame)
            self.stream._encoder_loop(slot)
        server.assert_not_called()
        thread.assert_not_called()
        self.assertTrue(self.stream.is_enabled())
        self.cv2.imencode.assert_called_once()
        self.assertIs(self.cv2.imencode.call_args.args[1], frame)
        self.assertEqual(received, [("overlay", b"jpeg")])

    def test_no_stream_or_recording_hook_drops_frames_without_starting_encoder(self):
        with mock.patch.object(self.stream, "_get_or_create_slot") as make_slot:
            self.stream.push("overlay", object())
        make_slot.assert_not_called()
        self.assertFalse(self.stream.is_enabled())

    def test_unhooking_recorder_disables_local_encoding(self):
        self.stream.set_recorder_hook(mock.Mock())
        self.assertTrue(self.stream.is_enabled())
        self.stream.set_recorder_hook(None)
        self.assertFalse(self.stream.is_enabled())
        with mock.patch.object(self.stream, "_get_or_create_slot") as make_slot:
            self.stream.push("overlay", object())
        make_slot.assert_not_called()

    def test_http_streaming_still_accepts_frames_without_recorder(self):
        self.stream._ENABLED = True
        slot = self.stream._Slot("overlay")
        frame = object()
        with mock.patch.object(self.stream, "_get_or_create_slot", return_value=slot):
            self.stream.push("overlay", frame)
        self.assertIs(slot.raw_queue.get_nowait(), frame)
        self.assertEqual(slot.frames_in, 1)


if __name__ == "__main__":
    unittest.main()
