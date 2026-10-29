"""Camera decoding regression tests: real NumPy/OpenCV, stubbed native SDK.

Run: python -m unittest discover -s tests/unit -p test_camera_decode.py -v
No camera, SDK DLL, network connection, or GUI is opened.
"""
import ctypes
from contextlib import redirect_stdout
import importlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import cv2
import numpy as np


def load_camera_modules():
    # The SDK's generated constants/structures are safe to import. Replace only
    # MvCameraControl_class, whose normal import would load a native DLL/.so.
    sdk_name = 'bottle_volume_control.camera.sdk.MvCameraControl_class'
    sdk = types.ModuleType(sdk_name)
    for name in ('PixelType_header', 'CameraParams_header'):
        header = importlib.import_module(f'bottle_volume_control.camera.sdk.{name}')
        for key, value in vars(header).items():
            if not key.startswith('_'):
                setattr(sdk, key, value)
    adapter = importlib.import_module('bottle_volume_control.camera.device')
    # Only the native binding is stubbed; exercise the installed package's real IO.
    with mock.patch.dict(sys.modules, {sdk_name: sdk}):
        camera_io = importlib.import_module('bottle_volume_control.camera.io')
    return camera_io, adapter


camera_io, camera_adapter = load_camera_modules()


def make_frame(data, pixel_type, width, height, frame_len=None):
    payload = np.asarray(data, dtype=np.uint8).tobytes()
    buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    return types.SimpleNamespace(
        _buffer=buffer,
        pBufAddr=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        stFrameInfo=types.SimpleNamespace(
            nWidth=width, nHeight=height, enPixelType=pixel_type,
            nFrameLen=len(payload) if frame_len is None else frame_len,
            nFrameNum=1, nLostPacket=0,
        ),
    )


class FakeCamera:
    def __init__(self):
        self.get_results = [0]
        self.timeouts = []
        self.freed = 0
        self.convert_calls = []
        self.convert_result = 0
        self.convert_output = b''
        self.hb_calls = 0
        self.hb_result = 0
        self.hb_output = b''
        self.hb_pixel_type = camera_io.PixelType_Gvsp_RGB8_Packed

    def MV_CC_GetImageBuffer(self, frame, timeout):
        self.timeouts.append(timeout)
        return self.get_results.pop(0)

    def MV_CC_FreeImageBuffer(self, frame):
        self.freed += 1
        return 0

    def MV_CC_ConvertPixelTypeEx(self, params):
        self.convert_calls.append((params.enSrcPixelType, params.enDstPixelType))
        if self.convert_result == 0:
            if len(self.convert_output) > params.nDstBufferSize:
                raise AssertionError('mock output exceeds destination')
            ctypes.memmove(params.pDstBuffer, self.convert_output, len(self.convert_output))
            params.nDstLen = len(self.convert_output)
        return self.convert_result

    def MV_CC_HBDecode(self, params):
        self.hb_calls += 1
        if self.hb_result == 0:
            if len(self.hb_output) > params.nDstBufSize:
                raise AssertionError('mock HB output exceeds destination')
            ctypes.memmove(params.pDstBuf, self.hb_output, len(self.hb_output))
            params.nDstBufLen = len(self.hb_output)
            params.enDstPixelType = self.hb_pixel_type
        return self.hb_result


class CameraDecodeTests(unittest.TestCase):
    def setUp(self):
        self.errors = mock.patch.object(camera_io.rt_util, 'print_error').start()
        mock.patch.object(camera_io.rt_util, 'print_info_throttle').start()
        self.sleep = mock.patch.object(camera_io.time, 'sleep').start()
        self.addCleanup(mock.patch.stopall)

    def decode(self, frame, cam=None):
        return camera_io.GrabCameraFrame(cam or FakeCamera(), frame)

    def test_mono8_is_owned_copy(self):
        expected = np.arange(6, dtype=np.uint8).reshape(2, 3)
        frame = make_frame(expected, camera_io.PixelType_Gvsp_Mono8, 3, 2)
        cam = FakeCamera()
        image, channels = self.decode(frame, cam)
        frame._buffer[0] = 99
        np.testing.assert_array_equal(image, expected)
        self.assertEqual(channels, 1)
        self.assertEqual(cam.convert_calls, [])
        self.assertEqual(cam.freed, 0)  # Caller still owns the acquired frame.

    def test_rgb8_and_bgr8_return_rgb_and_adapter_returns_bgr(self):
        expected_rgb = np.tile(np.array([240, 100, 20], dtype=np.uint8), (2, 2, 1))
        for pixel_type, data in (
            (camera_io.PixelType_Gvsp_RGB8_Packed, expected_rgb),
            (camera_io.PixelType_Gvsp_BGR8_Packed, expected_rgb[:, :, ::-1]),
        ):
            with self.subTest(pixel_type=pixel_type):
                frame = make_frame(data, pixel_type, 2, 2)
                image, channels = self.decode(frame)
                np.testing.assert_array_equal(image, expected_rgb)
                self.assertEqual(channels, 3)
                adapter = camera_adapter.Camera()
                adapter._cam, adapter._st_out, adapter._camera_io = FakeCamera(), frame, camera_io
                np.testing.assert_array_equal(adapter.grab(), expected_rgb[:, :, ::-1])

    def test_four_bayer_patterns_preserve_red_and_blue(self):
        colors = {'R': 240, 'G': 100, 'B': 20}
        for pixel_type, pattern in (
            (camera_io.PixelType_Gvsp_BayerRG8, 'RGGB'),
            (camera_io.PixelType_Gvsp_BayerGR8, 'GRBG'),
            (camera_io.PixelType_Gvsp_BayerGB8, 'GBRG'),
            (camera_io.PixelType_Gvsp_BayerBG8, 'BGGR'),
        ):
            with self.subTest(pattern=pattern):
                mosaic = np.empty((8, 8), dtype=np.uint8)
                for y in range(2):
                    for x in range(2):
                        mosaic[y::2, x::2] = colors[pattern[2 * y + x]]
                frame = make_frame(mosaic, pixel_type, 8, 8)
                cam = FakeCamera()
                image, channels = self.decode(frame, cam)
                expected = np.tile(np.array([240, 100, 20], dtype=np.uint8), (4, 4, 1))
                np.testing.assert_array_equal(image[2:-2, 2:-2], expected)
                self.assertEqual(channels, 3)
                self.assertEqual(cam.convert_calls, [])
                adapter = camera_adapter.Camera()
                adapter._cam, adapter._st_out, adapter._camera_io = FakeCamera(), frame, camera_io
                np.testing.assert_array_equal(adapter.grab()[2:-2, 2:-2], expected[:, :, ::-1])

    def test_mono16_uses_sdk_conversion_to_mono8(self):
        frame = make_frame(np.zeros(8, dtype=np.uint8), camera_io.PixelType_Gvsp_Mono16, 2, 2)
        cam = FakeCamera()
        cam.convert_output = bytes([10, 20, 30, 40])
        image, channels = self.decode(frame, cam)
        np.testing.assert_array_equal(image, [[10, 20], [30, 40]])
        self.assertEqual(channels, 1)
        self.assertEqual(cam.convert_calls, [(camera_io.PixelType_Gvsp_Mono16, camera_io.PixelType_Gvsp_Mono8)])

    def test_other_format_requires_successful_sdk_conversion(self):
        frame = make_frame([1, 2, 3, 4], 0x12345678, 1, 1)
        cam = FakeCamera()
        cam.convert_output = bytes([240, 100, 20])
        image, channels = self.decode(frame, cam)
        np.testing.assert_array_equal(image, [[[240, 100, 20]]])
        self.assertEqual(channels, 3)
        self.assertEqual(cam.convert_calls, [(0x12345678, camera_io.PixelType_Gvsp_RGB8_Packed)])

    def test_unsupported_format_is_reported_and_buffer_released(self):
        frame = make_frame([1, 2, 3, 4], 0x12345678, 1, 1)
        cam = FakeCamera()
        cam.convert_result = 0x80000000
        self.assertIsNone(self.decode(frame, cam))
        self.assertEqual(cam.freed, 1)
        self.assertIn('0x12345678', self.errors.call_args.args[0])

    def test_hb_decodes_before_using_direct_rgb_path(self):
        frame = make_frame([1], camera_io.PixelType_Gvsp_HB_RGB8_Packed, 1, 1)
        cam = FakeCamera()
        cam.hb_output = bytes([240, 100, 20])
        image, channels = self.decode(frame, cam)
        np.testing.assert_array_equal(image, [[[240, 100, 20]]])
        self.assertEqual(channels, 3)
        self.assertEqual(cam.hb_calls, 1)
        self.assertEqual(cam.convert_calls, [])

    def test_hb_high_bit_mono_still_uses_sdk_conversion(self):
        frame = make_frame([1], camera_io.PixelType_Gvsp_HB_Mono16, 1, 1)
        cam = FakeCamera()
        cam.hb_output = bytes([1, 2])
        cam.hb_pixel_type = camera_io.PixelType_Gvsp_Mono16
        cam.convert_output = bytes([77])
        image, channels = self.decode(frame, cam)
        np.testing.assert_array_equal(image, [[77]])
        self.assertEqual(channels, 1)
        self.assertEqual(cam.hb_calls, 1)
        self.assertEqual(len(cam.convert_calls), 1)

    def test_hb_decode_failure_releases_buffer(self):
        frame = make_frame([1], camera_io.PixelType_Gvsp_HB_RGB8_Packed, 1, 1)
        cam = FakeCamera()
        cam.hb_result = 0x80000000
        self.assertIsNone(self.decode(frame, cam))
        self.assertEqual(cam.freed, 1)
        self.assertEqual(cam.convert_calls, [])

    def test_short_frame_is_rejected_without_reading_past_buffer(self):
        frame = make_frame([1, 2], camera_io.PixelType_Gvsp_RGB8_Packed, 1, 1)
        cam = FakeCamera()
        self.assertIsNone(self.decode(frame, cam))
        self.assertEqual(cam.freed, 1)
        self.assertIn('shorter', self.errors.call_args.args[0])

    def test_incomplete_sdk_output_is_rejected(self):
        frame = make_frame([1, 2, 3, 4], camera_io.PixelType_Gvsp_Mono16, 2, 1)
        cam = FakeCamera()
        cam.convert_output = bytes([1])
        self.assertIsNone(self.decode(frame, cam))
        self.assertEqual(cam.freed, 1)

    def test_retries_do_not_free_unacquired_or_stale_buffer(self):
        frame = make_frame([1], camera_io.PixelType_Gvsp_Mono8, 1, 1)
        cam = FakeCamera()
        cam.get_results = [1, 1, 0]
        image, channels = self.decode(frame, cam)
        np.testing.assert_array_equal(image, [[1]])
        self.assertEqual(cam.timeouts, [3000, 3000, 3000])
        self.assertEqual(cam.freed, 0)
        self.assertEqual(self.sleep.call_count, 2)

    def test_exhausted_retries_return_none_without_free(self):
        frame = make_frame([1], camera_io.PixelType_Gvsp_Mono8, 1, 1)
        cam = FakeCamera()
        cam.get_results = [1, 1, 1]
        self.assertIsNone(self.decode(frame, cam))
        self.assertEqual(cam.timeouts, [3000, 3000, 3000])
        self.assertEqual(cam.freed, 0)

    def test_null_ctypes_pointer_is_not_a_successful_frame(self):
        frame = make_frame([1], camera_io.PixelType_Gvsp_Mono8, 1, 1)
        frame.pBufAddr = ctypes.POINTER(ctypes.c_ubyte)()
        cam = FakeCamera()
        cam.get_results = [0, 0, 0]
        self.assertIsNone(self.decode(frame, cam))
        self.assertEqual(cam.freed, 0)
        self.assertEqual(len(cam.timeouts), 3)


class CameraPackageTests(unittest.TestCase):
    def test_sdk_headers_and_device_import_without_loading_native_library(self):
        original_path = list(sys.path)
        with mock.patch.object(ctypes.cdll, 'LoadLibrary') as load_library, \
                mock.patch.object(ctypes, 'WinDLL', create=True) as win_dll:
            for name in (
                'bottle_volume_control.camera.sdk',
                'bottle_volume_control.camera.sdk.PixelType_header',
                'bottle_volume_control.camera.sdk.CameraParams_header',
                'bottle_volume_control.camera.device',
                'bottle_volume_control.camera.capture',
            ):
                importlib.reload(importlib.import_module(name))
        load_library.assert_not_called()
        win_dll.assert_not_called()
        self.assertEqual(sys.path, original_path)

    def test_capture_help_does_not_open_camera(self):
        capture = importlib.import_module('bottle_volume_control.camera.capture')
        output = io.StringIO()
        with mock.patch.object(capture, 'capture_one_frame') as capture_frame, redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                capture.main(['--help'])
        self.assertEqual(result.exception.code, 0)
        capture_frame.assert_not_called()
        self.assertIn('var/images', output.getvalue())

    def test_capture_default_output_uses_runtime_images_directory(self):
        capture = importlib.import_module('bottle_volume_control.camera.capture')
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp) / 'var' / 'images'
            with mock.patch.object(capture, 'images_dir', return_value=output_dir), \
                    mock.patch.object(capture, 'capture_one_frame', return_value=np.zeros((2, 2), dtype=np.uint8)), \
                    mock.patch.object(capture.cv2, 'imwrite', return_value=True) as write, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(capture.main([]), 0)
            self.assertEqual(Path(write.call_args.args[0]).parent, output_dir)

    def test_capture_success_returns_frame_once_and_closes_camera(self):
        capture = importlib.import_module('bottle_volume_control.camera.capture')
        package = importlib.import_module('bottle_volume_control.camera')
        expected = np.arange(4, dtype=np.uint8).reshape(2, 2)
        cam = FakeCamera()
        with mock.patch.object(package, 'io', camera_io, create=True), \
                mock.patch.object(camera_io, 'OpenCamera', return_value=cam), \
                mock.patch.object(camera_io, 'GrabCameraFrame', return_value=(expected, 1)), \
                mock.patch.object(camera_io, 'CloseCamera') as close:
            np.testing.assert_array_equal(capture.capture_one_frame(), expected)
        self.assertEqual(cam.freed, 1)
        close.assert_called_once_with(cam)

    def test_capture_conversion_exception_returns_frame_once_and_closes_camera(self):
        capture = importlib.import_module('bottle_volume_control.camera.capture')
        package = importlib.import_module('bottle_volume_control.camera')
        cam = FakeCamera()
        with mock.patch.object(package, 'io', camera_io, create=True), \
                mock.patch.object(camera_io, 'OpenCamera', return_value=cam), \
                mock.patch.object(camera_io, 'GrabCameraFrame', return_value=(np.zeros((2, 2, 3), dtype=np.uint8), 3)), \
                mock.patch.object(capture.cv2, 'cvtColor', side_effect=RuntimeError('conversion failed')), \
                mock.patch.object(camera_io, 'CloseCamera') as close:
            with self.assertRaisesRegex(RuntimeError, 'conversion failed'):
                capture.capture_one_frame()
        self.assertEqual(cam.freed, 1)
        close.assert_called_once_with(cam)

    def test_capture_failed_grab_does_not_return_an_unacquired_frame(self):
        capture = importlib.import_module('bottle_volume_control.camera.capture')
        package = importlib.import_module('bottle_volume_control.camera')
        cam = FakeCamera()
        with mock.patch.object(package, 'io', camera_io, create=True), \
                mock.patch.object(camera_io, 'OpenCamera', return_value=cam), \
                mock.patch.object(camera_io, 'GrabCameraFrame', return_value=None), \
                mock.patch.object(camera_io, 'CloseCamera') as close, redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                capture.capture_one_frame(max_attempts=1)
        self.assertEqual(cam.freed, 0)
        close.assert_called_once_with(cam)


if __name__ == '__main__':
    unittest.main()
