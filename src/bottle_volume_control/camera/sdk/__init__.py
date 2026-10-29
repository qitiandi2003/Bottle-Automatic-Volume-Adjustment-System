"""Bundled MVS bindings: importing this package or its headers loads no DLL.

Only explicitly importing MvCameraControl_class loads the native SDK.
"""
from importlib import import_module


__version__ = '4.6.0.1'
__all__ = [
    'MvErrorDefine_const', 'MvISPErrorDefine_const', 'PixelType_header',
    'CameraParams_const', 'CameraParams_header', 'MvCameraControl_class',
]


def __getattr__(name):
    if name in __all__:
        module = import_module(f'.{name}', __name__)
        globals()[name] = module
        return module
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__():
    return sorted(set(globals()) | set(__all__))
