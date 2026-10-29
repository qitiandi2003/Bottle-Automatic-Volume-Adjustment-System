# -*- coding: utf-8 -*-
# =============================================================================
# camera_io.py —— 海康工业相机 IO 封装
# -----------------------------------------------------------------------------
# 本模块是对海康 MV SDK (MvCameraControl) 的薄封装，暴露三个函数给上层使用：
#   OpenCamera()                     —— 枚举设备、打开、配置、开始取流，返回相机句柄
#   CloseCamera(cam)                 —— 停止取流、关闭设备、销毁句柄、反初始化 SDK
#   GrabCameraFrame(cam, stOutFrame) —— 取一帧、必要时 HB 解码、转 RGB/Mono，返回 numpy
# 两个判别函数：
#   IsHBPixelFormat / IsMonoPixelFormat —— 识别像素格式分类，决定解码与转换路径
# =============================================================================
import sys
import os
import time
from ctypes import c_ubyte
import rt_config                     # 里面已经把 MvImport 路径加入 sys.path
import rt_util                       # print_error / print_info_throttle 等工具
import numpy
import cv2


from MvCameraControl_class import *  # 从 MvImport 导入 SDK 全部符号（常量、结构体、类）


_HB_PIXEL_TYPES = frozenset(
    value for name, value in globals().copy().items()
    if name.startswith('PixelType_Gvsp_HB_')
)


def IsHBPixelFormat(enPixelType=0):
    """识别本版本 SDK 定义的 HB 压缩格式；必须先解压才能读取像素。"""
    return (enPixelType & 0xffffffff) in _HB_PIXEL_TYPES


def IsMonoPixelFormat(enPixelType = 0):
    """判断是否为单通道（灰度）像素类型。
    用于决定 MV_CC_ConvertPixelTypeEx 目标格式：Mono → Mono8, 否则 → RGB8_Packed。
    """
    if enPixelType in (PixelType_Gvsp_Mono8, \
                        PixelType_Gvsp_Mono10, \
                        PixelType_Gvsp_Mono10_Packed, \
                        PixelType_Gvsp_Mono12, \
                        PixelType_Gvsp_Mono12_Packed, \
                        PixelType_Gvsp_Mono14, \
                        PixelType_Gvsp_Mono16):
        return True
    else:
        return False

def OpenCamera():
    """一条龙：SDK 初始化 → 枚举设备 → 选 → 创建句柄 → OpenDevice → 配参 → StartGrab。

    返回 MvCamera 实例；失败时 sys.exit() 让主程序直接退出。
    由于本项目只做单相机，不需要返回多设备管理器。
    """
    MvCamera.MV_CC_Initialize()

    deviceList = MV_CC_DEVICE_INFO_LIST()
    # 枚举协议集合：GigE / USB3 / CameraLink / CoaXPress / XoF 全开
    tlayerType = (MV_GIGE_DEVICE | MV_USB_DEVICE | MV_GENTL_CAMERALINK_DEVICE
                  | MV_GENTL_CXP_DEVICE | MV_GENTL_XOF_DEVICE)

    # ch:枚举设备 | en:Enum device
    ret = MvCamera.MV_CC_EnumDevices(tlayerType, deviceList)
    if ret != 0:
        rt_util.print_error("enum devices fail! ret[0x%x]" % ret)
        sys.exit()

    if deviceList.nDeviceNum == 0:
        rt_util.print_error("find no device!")
        sys.exit()

    print ("Find %d devices!" % deviceList.nDeviceNum)

    # 打印每台设备的简要信息：型号、IP/SN —— 方便用户确认
    for i in range(0, deviceList.nDeviceNum):
        # pDeviceInfo[i] 是 C 指针，cast + contents 转成 Python 可访问的结构体
        mvcc_dev_info = cast(deviceList.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents
        if mvcc_dev_info.nTLayerType == MV_GIGE_DEVICE or mvcc_dev_info.nTLayerType == MV_GENTL_GIGE_DEVICE:
            print ("\ngige device: [%d]" % i)
            # chModelName 是定长 char 数组，过滤掉 0 字节再拼成字符串
            strModeName = ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stGigEInfo.chModelName if c != 0])
            print ("device model name: %s" % strModeName)

            # 当前 IP 是 uint32，手动拆成 4 字节点分式
            # 公式： ip_ij = (nCurrentIp >> (8 * (3-j))) & 0xFF
            nip1 = ((mvcc_dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0xff000000) >> 24)
            nip2 = ((mvcc_dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0x00ff0000) >> 16)
            nip3 = ((mvcc_dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0x0000ff00) >> 8)
            nip4 = (mvcc_dev_info.SpecialInfo.stGigEInfo.nCurrentIp & 0x000000ff)
            print ("current ip: %d.%d.%d.%d\n" % (nip1, nip2, nip3, nip4))
        elif mvcc_dev_info.nTLayerType == MV_USB_DEVICE:
            print ("\nu3v device: [%d]" % i)
            strModeName = ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stUsb3VInfo.chModelName if c != 0])
            print ("device model name: %s" % strModeName)

            strSerialNumber =  ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stUsb3VInfo.chSerialNumber if c != 0])
            print ("user serial number: %s" % strSerialNumber)
        elif mvcc_dev_info.nTLayerType == MV_GENTL_CAMERALINK_DEVICE:
            print ("\nCML device: [%d]" % i)
            strModeName = ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stCMLInfo.chModelName if c != 0])
            print ("device model name: %s" % strModeName)

            strSerialNumber = ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stCMLInfo.chSerialNumber if c != 0])
            print ("user serial number: %s" % strSerialNumber)
        elif mvcc_dev_info.nTLayerType == MV_GENTL_CXP_DEVICE:
            print ("\nCXP device: [%d]" % i)
            strModeName =  ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stCXPInfo.chModelName if c != 0])
            print ("device model name: %s" % strModeName)

            strSerialNumber =  ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stCXPInfo.chSerialNumber if c != 0])
            print ("user serial number: %s" % strSerialNumber)
        elif mvcc_dev_info.nTLayerType == MV_GENTL_XOF_DEVICE:
            print ("\nXoF device: [%d]" % i)
            strModeName =  ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stXoFInfo.chModelName if c != 0])
            print ("device model name: %s" % strModeName)

            strSerialNumber =  ''.join([chr(c) for c in mvcc_dev_info.SpecialInfo.stXoFInfo.chSerialNumber if c != 0])
            print ("user serial number: %s" % strSerialNumber)

    # 若仅有 1 台设备，默认选择 0；否则交互输入
    # 避免无人值守启动时卡在 input 等待
    if deviceList.nDeviceNum == 1:
        nConnectionNum = "0"
        print("only one device found; auto select 0")
    else:
        nConnectionNum = input("please input the number of the device to connect:")

    if int(nConnectionNum) >= deviceList.nDeviceNum:
        rt_util.print_error("intput error!")
        sys.exit()

    cam = MvCamera()

    # ch:选择设备并创建句柄 | en:Select device and create handle
    stDeviceList = cast(deviceList.pDeviceInfo[int(nConnectionNum)], POINTER(MV_CC_DEVICE_INFO)).contents

    ret = cam.MV_CC_CreateHandle(stDeviceList)
    if ret != 0:
        rt_util.print_error("create handle fail! ret[0x%x]" % ret)
        sys.exit()

    # ch:打开设备 | en:Open device
    # MV_ACCESS_Exclusive：独占打开，避免其他进程同时抓图造成帧丢失
    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    if ret != 0:
        rt_util.print_error("open device fail! ret[0x%x]" % ret)
        sys.exit()
        
    # =========================================================================
    # GigE 保守配置（RDK X5 / ARM 板子稳定性优化）
    # 不使用 SDK 自动探测 jumbo packet
    # =========================================================================
    if (
        stDeviceList.nTLayerType == MV_GIGE_DEVICE
        or
        stDeviceList.nTLayerType == MV_GENTL_GIGE_DEVICE
    ):

        # 强制使用标准 MTU 对应 packet size
        ret = cam.MV_CC_SetIntValue(
            "GevSCPSPacketSize",
            1500
        )

        print(
            "[GigE] set packet size ret = 0x%x"
            % ret
        )

        # 增加包间隔
        # 防止 ARM 网卡 DMA / 中断被打爆
        ret = cam.MV_CC_SetIntValue(
            "GevSCPD",
            1000
        )

        print(
            "[GigE] set GevSCPD ret = 0x%x"
            % ret
        )

        # 设置帧率（目前输出15fps）
        ret = cam.MV_CC_SetFloatValue(
            "AcquisitionFrameRate",
            15
        )

        print(
            "[GigE] set fps ret = 0x%x"
            % ret
        )

    # ch:设置触发模式为off | en:Set trigger mode as off
    # 关闭外触发 → 连续自由取流
    ret = cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
    if ret != 0:
        rt_util.print_error("set trigger mode fail! ret[0x%x]" % ret)
        sys.exit()

    # 启动时设置手动曝光为固定值（仅一次），随后仅在首次检测到试管后自动调一次
    # 和 auto_exposure 的"one-shot"策略配套：相机一打开就是一个已知的基线
    if rt_config.EXP_BOOT_ENABLED:
        cam.MV_CC_SetEnumValue("ExposureAuto", 0)                                # 关闭自动曝光
        cam.MV_CC_SetFloatValue("ExposureTime", float(rt_config.EXP_BOOT_US))    # 手动写入初值
        print(f"[expo-boot] set manual {rt_config.EXP_BOOT_US:.0f}us")

    # ch:开始取流 | en:Start grab image
    ret = cam.MV_CC_StartGrabbing()
    if ret != 0:
        rt_util.print_error("start grabbing fail! ret[0x%x]" % ret)
        sys.exit()

    return cam

def CloseCamera(cam):
    """对称的关闭流程：停取流 → 关设备 → 销毁句柄 → SDK 反初始化。"""
    # ch:停止取流 | en:Stop grab image
    ret = cam.MV_CC_StopGrabbing()
    if ret != 0:
        rt_util.print_error("stop grabbing fail! ret[0x%x]" % ret)
        sys.exit()

    # ch:关闭设备 | Close device
    ret = cam.MV_CC_CloseDevice()
    if ret != 0:
        rt_util.print_error("close deivce fail! ret[0x%x]" % ret)
        sys.exit()

    # ch:销毁句柄 | Destroy handle
    ret = cam.MV_CC_DestroyHandle()
    if ret != 0:
        rt_util.print_error("destroy handle fail! ret[0x%x]" % ret)
        sys.exit()

    # ch:反初始化SDK | en: finalize SDK
    # 必须在所有句柄销毁后才能反初始化，否则可能卡在资源回收
    MvCamera.MV_CC_Finalize()

    print("success close camera")


_BAYER8_RGB_CODES = {
    PixelType_Gvsp_BayerGR8: cv2.COLOR_BayerGRBG2RGB,
    PixelType_Gvsp_BayerRG8: cv2.COLOR_BayerRGGB2RGB,
    PixelType_Gvsp_BayerGB8: cv2.COLOR_BayerGBRG2RGB,
    PixelType_Gvsp_BayerBG8: cv2.COLOR_BayerBGGR2RGB,
}


def _decode_camera_buffer(cam, source, frame_len, width, height, pixel_type):
    """解码为独立的 Mono8/RGB8 数组，保持 Camera.grab() 的 RGB 输入契约。

    常见 8 位格式绕过 SDK 转换，保留 ARM 路径；HB 先解压，其他格式由 SDK
    明确转换。无法转换时抛出异常，由 GrabCameraFrame 记录错误并归还缓冲区。
    """
    pixel_type &= 0xffffffff  # 同时兼容 SDK 返回有符号/无符号枚举。
    if width <= 0 or height <= 0 or frame_len <= 0 or not source:
        raise ValueError('invalid camera frame dimensions or buffer')

    if IsHBPixelFormat(pixel_type):
        decoded = MV_CC_HB_DECODE_PARAM()
        # SDK 支持的最大 HB 格式是 RGBA16：每像素 8 字节。
        decode_buffer = (c_ubyte * (width * height * 8))()
        decoded.pSrcBuf = source
        decoded.nSrcLen = frame_len
        decoded.pDstBuf = decode_buffer
        decoded.nDstBufSize = len(decode_buffer)
        ret = cam.MV_CC_HBDecode(decoded)
        if ret != 0:
            raise ValueError('HB decode failed: 0x%08x' % (ret & 0xffffffff))
        if (decoded.nDstBufLen <= 0
                or decoded.nDstBufLen > len(decode_buffer)
                or IsHBPixelFormat(decoded.enDstPixelType)):
            raise ValueError('HB decoder returned invalid output')
        return _decode_camera_buffer(
            cam, decoded.pDstBuf, decoded.nDstBufLen,
            decoded.nWidth or width, decoded.nHeight or height,
            decoded.enDstPixelType,
        )

    channels = 3 if pixel_type in (
        PixelType_Gvsp_RGB8_Packed, PixelType_Gvsp_BGR8_Packed,
    ) else 1
    if (pixel_type in (PixelType_Gvsp_Mono8, PixelType_Gvsp_RGB8_Packed,
                       PixelType_Gvsp_BGR8_Packed)
            or pixel_type in _BAYER8_RGB_CODES):
        required = width * height * channels
        if frame_len < required:
            raise ValueError('camera frame buffer is shorter than its pixel dimensions')
        raw = numpy.ctypeslib.as_array(source, shape=(required,))
        if pixel_type == PixelType_Gvsp_Mono8:
            return raw.reshape(height, width).copy(), 1
        if channels == 3:
            image = raw.reshape(height, width, 3)
            if pixel_type == PixelType_Gvsp_BGR8_Packed:
                image = image[:, :, ::-1]
            return image.copy(), 3
        # 四字母常量直接表达传感器排列，避免两字母 Bayer 别名的颜色歧义。
        return cv2.cvtColor(
            raw.reshape(height, width), _BAYER8_RGB_CODES[pixel_type]
        ), 3

    # 高位 Mono/Bayer、YUV 等保留 v1 的 SDK 通用转换能力。
    # 不认识的格式必须由 SDK 接受，绝不猜成 BayerRG8。
    converted = MV_CC_PIXEL_CONVERT_PARAM_EX()
    channels = 1 if IsMonoPixelFormat(pixel_type) else 3
    destination = (c_ubyte * (width * height * channels))()
    converted.nWidth = width
    converted.nHeight = height
    converted.pSrcData = source
    converted.nSrcDataLen = frame_len
    converted.enSrcPixelType = pixel_type
    converted.enDstPixelType = (
        PixelType_Gvsp_Mono8 if channels == 1 else PixelType_Gvsp_RGB8_Packed
    )
    converted.pDstBuffer = destination
    converted.nDstBufferSize = len(destination)
    ret = cam.MV_CC_ConvertPixelTypeEx(converted)
    if ret != 0:
        raise ValueError('SDK conversion failed for pixel type 0x%08x: 0x%08x'
                         % (pixel_type & 0xffffffff, ret & 0xffffffff))
    if converted.nDstLen != len(destination):
        raise ValueError('SDK conversion returned an incomplete frame')
    shape = (height, width) if channels == 1 else (height, width, 3)
    return numpy.frombuffer(destination, dtype=numpy.uint8).reshape(shape).copy(), channels


def GrabCameraFrame(cam, stOutFrame):
    """取帧后返回 (Mono8/RGB8 numpy, 通道数)，最多尝试 3 次，每次超时 3 秒。

    成功时由调用方归还 SDK 帧缓冲；解码失败时在此归还并返回 None。
    """
    max_retry = 3
    timeout_ms = 3000  # 增大超时时间，单位毫秒

    for attempt in range(max_retry):
        # 1) MV_CC_GetImageBuffer 阻塞取一帧
        ret = cam.MV_CC_GetImageBuffer(stOutFrame, timeout_ms)
        
        # 检查是否成功获取数据
        if ret == 0 and bool(stOutFrame.pBufAddr):
            break # 成功获取，跳出重试循环
        else:
            # 打印错误信息，包含尝试次数
            rt_util.print_error("no data[0x%x] (attempt %d/%d)" % (ret, attempt + 1, max_retry))
            # 取帧失败并未取得缓冲区所有权，不能 free 上一次帧留下的指针。
            if attempt + 1 < max_retry:
                time.sleep(0.1) # 短暂休眠，避免高速空转
    else:
        # 所有重试均失败
        rt_util.print_error("Grab failed after %d retries, giving up." % max_retry)
        return None

    # 10 秒节流打印一次帧信息，避免循环狂刷日志
    rt_util.print_info_throttle("get one frame: Width[%d], Height[%d], nFrameNum[%d], nLostPacket[%d]" % ( 
        stOutFrame.stFrameInfo.nWidth, stOutFrame.stFrameInfo.nHeight, 
        stOutFrame.stFrameInfo.nFrameNum, stOutFrame.stFrameInfo.nLostPacket), gap_s=10.0)

    info = stOutFrame.stFrameInfo
    try:
        return _decode_camera_buffer(
            cam, stOutFrame.pBufAddr, info.nFrameLen,
            info.nWidth, info.nHeight, info.enPixelType,
        )
    except Exception as exc:
        rt_util.print_error('camera frame decode failed: %s' % exc)
        cam.MV_CC_FreeImageBuffer(stOutFrame)
        return None
    

if __name__ == '__main__':
    # 自测入口：连续取 10 帧，每帧显示到窗口，用于验证 SDK/相机是否工作
    cam = OpenCamera()

    # stOutFrame 是帧信息结构体，memset 清零确保初始状态一致
    stOutFrame = MV_FRAME_OUT()
    memset(byref(stOutFrame), 0, sizeof(stOutFrame))

    error_idx = 0
    while error_idx < 10:
        result = GrabCameraFrame(cam, stOutFrame)
        if result is None:
            error_idx += 1
            continue
        numpy_image, nChannelNum = result

        print(nChannelNum)
        # 这里记得释放相机侧的 image buffer（不是 DstBuffer）
        cam.MV_CC_FreeImageBuffer(stOutFrame)

        # SDK 给的是 RGB，OpenCV 习惯 BGR；imshow 才会颜色对
        bgr_image = (cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)
                     if nChannelNum == 3 else numpy_image)

        cv2.imshow('bgr', bgr_image)
        cv2.waitKey(1)              # 让 GUI 事件循环跑一下

        error_idx+=1

    CloseCamera(cam)
