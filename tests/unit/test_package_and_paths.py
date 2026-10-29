"""Check public entry points and generated-data paths without camera hardware."""
import contextlib
import importlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bottle_volume_control import app, paths
from bottle_volume_control.diagnostics.logger import Asynclogger
from bottle_volume_control.diagnostics.recorder import VizRecorder
from bottle_volume_control.diagnostics import stream


class PackageAndPathsTests(unittest.TestCase):
    def test_help_exits_before_starting_application(self):
        with mock.patch.object(app, "main") as main, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                app.cli(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("usage:", output.getvalue())
        main.assert_not_called()

    def test_invalid_arguments_do_not_start_hardware(self):
        with mock.patch.object(app, "main") as main, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                app.cli(["--unknown-option"])
        self.assertEqual(result.exception.code, 2)
        main.assert_not_called()

    def test_application_entry_point_propagates_exit_status(self):
        with mock.patch.object(app, "main", return_value=7) as main:
            self.assertEqual(app.cli([]), 7)
        main.assert_called_once_with()

    def test_generated_data_defaults_to_working_directory(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BVC_DATA_DIR", None)
            original = Path.cwd()
            try:
                os.chdir(temp)
                self.assertEqual(paths.logs_dir(), Path(temp).resolve() / "var" / "logs")
                self.assertEqual(paths.images_dir(), Path(temp).resolve() / "var" / "images")
            finally:
                os.chdir(original)

    def test_logger_stream_and_recorder_share_overridden_data_directory(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"BVC_DATA_DIR": temp}):
            logger = Asynclogger(print_to_console=False)
            recorder = VizRecorder()
            expected = str(Path(temp).resolve() / "logs")
            self.assertEqual(logger.get_logs_dir(), expected)
            self.assertEqual(stream._get_logs_dir(), expected)
            self.assertEqual(recorder._base_dir, expected)
            self.assertTrue(Path(expected).is_dir())

    def test_sdk_headers_can_load_without_native_library(self):
        # Importing only generated constants must not load a driver DLL/.so.
        with mock.patch("ctypes.CDLL", side_effect=AssertionError("native library loaded")):
            header = importlib.import_module("bottle_volume_control.camera.sdk.CameraParams_header")
            pixel = importlib.import_module("bottle_volume_control.camera.sdk.PixelType_header")
        self.assertTrue(hasattr(header, "MV_FRAME_OUT"))
        self.assertTrue(hasattr(pixel, "PixelType_Gvsp_Mono8"))


if __name__ == "__main__":
    unittest.main()
