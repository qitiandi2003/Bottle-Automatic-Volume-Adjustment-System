"""Recorder regressions using temporary files and mocked ffmpeg, without the SDK."""
import importlib.util
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock


_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "viz_recorder_under_test", _ROOT / "framework" / "viz_recorder.py",
)
recorder_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recorder_module)
VizRecorder = recorder_module.VizRecorder


class VizRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.logger = mock.Mock()

    def make_recorder(self, **kwargs):
        return VizRecorder(base_dir=str(self.base), logger=self.logger, **kwargs)

    def test_encoded_frames_only_save_while_a_run_is_open(self):
        recorder = self.make_recorder()
        recorder.on_encoded("overlay", b"ignored")
        self.assertEqual(list(self.base.iterdir()), [])
        recorder.open_run("run_one")
        recorder.on_encoded("overlay", b"jpeg-one")
        recorder.close_run()
        recorder.on_encoded("overlay", b"ignored-after-close")
        stream = self.base / "run_one" / "overlay"
        self.assertEqual([p.name for p in stream.iterdir()], ["frame_000001.jpg"])
        self.assertEqual((stream / "frame_000001.jpg").read_bytes(), b"jpeg-one")

    def test_queued_runs_survive_age_count_and_size_limits(self):
        recorder = self.make_recorder(retain_runs=1, max_age_s=1, max_total_mb=0)
        for name in ("run_one", "run_two"):
            recorder.open_run(name)
            recorder.on_encoded("overlay", b"jpeg")
            recorder.close_run()
            os.utime(self.base / name, (1, 1))
        recorder.open_run("run_current")
        self.assertTrue((self.base / "run_one" / "overlay").is_dir())
        self.assertTrue((self.base / "run_two" / "overlay").is_dir())
        self.assertEqual(recorder._pending_finalize_rids, {"run_one", "run_two"})

    def test_dequeued_run_is_protected_during_wait_and_cleanup_resumes_afterward(self):
        recorder = self.make_recorder(retain_runs=1)
        recorder.open_run("run_previous")
        recorder.on_encoded("overlay", b"jpeg")
        recorder.close_run()
        os.utime(self.base / "run_previous", (1, 1))
        recorder._stop_event.set()

        def while_waiting(_seconds):
            self.assertEqual(recorder._pending_finalize_rids, set())
            self.assertEqual(recorder._finalizing_rids, {"run_previous"})
            recorder.open_run("run_current")
            self.assertTrue((self.base / "run_previous" / "overlay").is_dir())

        def finalize(run_id):
            self.assertEqual(run_id, "run_previous")
            recorder._retention_sweep()
            self.assertTrue((self.base / run_id / "overlay").is_dir())

        with mock.patch.object(recorder_module.time, "sleep", side_effect=while_waiting), \
                mock.patch.object(recorder, "_finalize_run", side_effect=finalize):
            recorder._finalizer_loop()
        self.assertEqual(recorder._finalizing_rids, set())
        self.assertEqual(recorder._finalize_queue.unfinished_tasks, 0)
        self.assertFalse((self.base / "run_previous").exists())
        self.assertTrue((self.base / "run_current").is_dir())

    def test_queue_failure_preserves_current_run_for_retry(self):
        recorder = self.make_recorder()
        recorder.open_run("run_one")
        with mock.patch.object(recorder._finalize_queue, "put_nowait", side_effect=queue.Full):
            recorder.close_run()
        self.assertEqual(recorder._current_run_id, "run_one")
        self.assertEqual(recorder._pending_finalize_rids, set())
        recorder.close_run()
        self.assertEqual(recorder._pending_finalize_rids, {"run_one"})

    def test_single_frame_is_kept_without_running_ffmpeg(self):
        recorder = self.make_recorder()
        recorder.open_run("run_one")
        recorder.on_encoded("profile", b"jpeg")
        with mock.patch.object(recorder_module.subprocess, "Popen") as popen:
            recorder._finalize_run("run_one")
        popen.assert_not_called()
        self.assertEqual((self.base / "run_one" / "profile.jpg").read_bytes(), b"jpeg")
        self.assertFalse((self.base / "run_one" / "profile").exists())

    def test_successful_ffmpeg_output_replaces_source_frames(self):
        recorder = self.make_recorder()
        recorder.open_run("run_one")
        recorder.on_encoded("overlay", b"one")
        recorder.on_encoded("overlay", b"two")
        process = mock.Mock(returncode=0)

        def finish_encoding(**_kwargs):
            (self.base / "run_one" / "overlay.mp4.partial").write_bytes(b"mp4")
            return b"", b""

        process.communicate.side_effect = finish_encoding
        with mock.patch.object(recorder_module.subprocess, "Popen", return_value=process) as popen:
            recorder._finalize_run("run_one")
        self.assertEqual(popen.call_args.args[0][0], "ffmpeg")
        self.assertEqual((self.base / "run_one" / "overlay.mp4").read_bytes(), b"mp4")
        self.assertFalse((self.base / "run_one" / "overlay").exists())

    def test_missing_ffmpeg_preserves_original_frames(self):
        recorder = self.make_recorder()
        recorder.open_run("run_one")
        recorder.on_encoded("overlay", b"one")
        recorder.on_encoded("overlay", b"two")
        with mock.patch.object(recorder_module.subprocess, "Popen", side_effect=FileNotFoundError):
            recorder._finalize_run("run_one")
        frames = list((self.base / "run_one" / "overlay").glob("*.jpg"))
        self.assertEqual(len(frames), 2)
        self.assertFalse((self.base / "run_one" / "overlay.mp4").exists())

    def test_shutdown_timeout_cancels_later_streams_and_queued_runs(self):
        recorder = self.make_recorder(retain_runs=1, max_total_mb=0)
        for run, streams in (("run_one", ("first", "second")), ("run_two", ("third",))):
            recorder.open_run(run)
            for stream in streams:
                recorder.on_encoded(stream, b"one")
                recorder.on_encoded(stream, b"two")
            recorder.close_run()
        communicating = threading.Event()
        released = threading.Event()
        process = mock.Mock(returncode=-15)

        def blocked_encode(**_kwargs):
            communicating.set()
            if not released.wait(5):
                raise AssertionError("test process was not released")
            return b"", b"cancelled"

        process.communicate.side_effect = blocked_encode
        process.terminate.side_effect = released.set
        with mock.patch.object(recorder_module, "FINALIZE_PRE_DELAY_S", 0), \
                mock.patch.object(recorder_module.subprocess, "Popen", return_value=process) as popen:
            recorder.start()
            thread = recorder._finalizer_thread
            try:
                self.assertTrue(communicating.wait(2))
                recorder.stop(drain_timeout=0.01)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(recorder._finalizer_thread)
                process.terminate.assert_called_once()
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(len(list(self.base.glob("run_*/*/frame_*.jpg"))), 6)
                self.assertEqual(recorder._pending_finalize_rids, {"run_one", "run_two"})
                recorder._retention_sweep()
                self.assertEqual(len(list(self.base.glob("run_*/*/frame_*.jpg"))), 6)
                # Even a direct caller cannot spawn ffmpeg after cancellation.
                self.assertFalse(recorder._ffmpeg_encode(
                    str(self.base / "run_two" / "third"), str(self.base / "run_two"), "third",
                ))
                self.assertEqual(popen.call_count, 1)
            finally:
                released.set()
                recorder._stop_event.set()
                thread.join(timeout=2)

    def test_normal_shutdown_drains_all_runs_without_cancelling(self):
        recorder = self.make_recorder()
        for run in ("run_one", "run_two"):
            recorder.open_run(run)
            recorder.on_encoded("overlay", b"one")
            recorder.on_encoded("overlay", b"two")
            recorder.close_run()

        def spawn(cmd, **_kwargs):
            process = mock.Mock(returncode=0)

            def finish(**_communicate_kwargs):
                Path(cmd[-1]).write_bytes(b"mp4")
                return b"", b""

            process.communicate.side_effect = finish
            return process

        with mock.patch.object(recorder_module, "FINALIZE_PRE_DELAY_S", 0), \
                mock.patch.object(recorder_module.subprocess, "Popen", side_effect=spawn) as popen:
            recorder.start()
            recorder.stop(drain_timeout=2)
        self.assertEqual(popen.call_count, 2)
        self.assertIsNone(recorder._finalizer_thread)
        self.assertFalse(recorder._abort_finalize.is_set())
        self.assertEqual(recorder._finalize_queue.unfinished_tasks, 0)
        self.assertEqual(len(list(self.base.glob("run_*/overlay.mp4"))), 2)
        self.assertEqual(list(self.base.glob("run_*/*/frame_*.jpg")), [])

    def test_shutdown_preserves_reference_when_process_will_not_exit(self):
        recorder = self.make_recorder()
        recorder.open_run("run_one")
        recorder.on_encoded("overlay", b"one")
        recorder.on_encoded("overlay", b"two")
        communicating = threading.Event()
        released = threading.Event()
        process = mock.Mock(returncode=-9)

        def blocked_encode(**_kwargs):
            communicating.set()
            if not released.wait(5):
                raise AssertionError("test process was not released")
            return b"", b"cancelled"

        process.communicate.side_effect = blocked_encode
        with mock.patch.object(recorder_module, "FINALIZE_PRE_DELAY_S", 0), \
                mock.patch.object(recorder_module.subprocess, "Popen", return_value=process):
            recorder.start()
            recorder.close_run()
            thread = recorder._finalizer_thread
            try:
                self.assertTrue(communicating.wait(2))
                # Avoid real timeout waits; deliberately leave the mock process alive.
                with mock.patch.object(thread, "join"):
                    recorder.stop(drain_timeout=0.01)
                self.assertIs(recorder._finalizer_thread, thread)
                self.assertTrue(thread.is_alive())
                process.terminate.assert_called_once()
                process.kill.assert_called_once()
                recorder.start()  # Must not launch a second finalizer.
                self.assertIs(recorder._finalizer_thread, thread)
                self.assertTrue(recorder._abort_finalize.is_set())
                self.assertEqual(len(list(self.base.glob("run_*/*/frame_*.jpg"))), 2)
            finally:
                released.set()
                recorder._stop_event.set()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
