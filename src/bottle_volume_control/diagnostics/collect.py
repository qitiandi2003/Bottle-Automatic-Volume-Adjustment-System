#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# collect_debug_data.py —— 把 save_images 目录归档到 data_NNN/ 的小工具
# -----------------------------------------------------------------------------
# 用途：
#   运行时所有中间图像都落到 save_images/ 下；这个脚本做"一键归档"：
#     - 扫描 --dest-root 下已经存在的 data_001/data_002/... 或纯数字目录；
#     - 选一个未占用的下一个序号 data_NNN；
#     - 把 --src（默认 save_images）里的所有文件/子目录完整复制过去。
#   目的是每次测试完都能分别保存一份现场数据做对比。
# 典型用法：
#   python collect_debug_data.py --dest-root /home/user/data_bank
# =============================================================================

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path
from ..paths import images_dir


def _extract_existing_indices(dest_root: Path) -> list[int]:
    """扫描 dest_root 下的直接子目录，找出已用过的序号。

    支持两种命名：
      - data_123  （推荐）
      - 123       （纯数字目录，兼容历史数据）
    """
    indices: list[int] = []
    if not dest_root.exists():
        return indices       # 目录不存在 → 空列表
    if not dest_root.is_dir():
        # 存在但不是目录，报错让用户立刻发现路径冲突
        raise NotADirectoryError(f"--dest-root 不是目录: {dest_root}")

    for p in dest_root.iterdir():
        if not p.is_dir():
            continue
        name = p.name
        # 匹配 data_123 形式
        m = re.fullmatch(r"data_(\d+)", name)
        if m:
            indices.append(int(m.group(1)))
            continue
        # 匹配纯数字目录
        m2 = re.fullmatch(r"(\d+)", name)
        if m2:
            indices.append(int(m2.group(1)))
            continue
    return indices


def _next_data_dir_name(dest_root: Path, width: int = 3) -> str:
    """返回下一个可用的目录名，形如 'data_004'。

    公式：  next_idx = max(existing) + 1    (为空时从 1 开始)
    填零宽度：width=3 → 'data_004'、'data_042'、'data_123'
    """
    existing = _extract_existing_indices(dest_root)
    next_idx = (max(existing) + 1) if existing else 1
    # f-string 的格式规则：:0{width}d 让数字左补 0 到 width 位
    return f"data_{next_idx:0{width}d}"


def _copy_contents(src_dir: Path, dst_dir: Path) -> None:
    """把 src_dir 下所有项目复制到 dst_dir（dst_dir 必须不存在）。

    - 目录用 copytree 递归复制
    - 文件用 copy2 保留元数据（mtime/权限）
    """
    # exist_ok=False：目标目录不能已存在，强制一份干净数据
    dst_dir.mkdir(parents=True, exist_ok=False)
    for item in src_dir.iterdir():
        src = item
        dst = dst_dir / item.name
        if item.is_dir():
            # dirs_exist_ok=False: 若对应子目录已存在会抛 FileExistsError
            shutil.copytree(src, dst, dirs_exist_ok=False)
        else:
            shutil.copy2(src, dst)


def main() -> int:
    """CLI 入口。成功返回 0。"""
    parser = argparse.ArgumentParser(
        description="把运行图片复制到指定目录下的新 data_{num} 文件夹中（自动递增序号）"
    )
    parser.add_argument(
        "--src",
        default=str(images_dir()),
        help="源目录（默认：var/images，可用 BVC_DATA_DIR 覆盖）",
    )
    parser.add_argument(
        "--dest-root",
        required=True,
        help="目标根目录（会在该目录下创建 data_001/data_002/...）",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=3,
        help="序号位数（默认 3：001/002/003）",
    )
    args = parser.parse_args()

    # 处理路径：expanduser 展开 ~；resolve 取绝对路径
    src_dir = Path(args.src).expanduser().resolve()
    dest_root = Path(args.dest_root).expanduser().resolve()

    if not src_dir.exists():
        raise FileNotFoundError(f"--src 不存在: {src_dir}")
    if not src_dir.is_dir():
        raise NotADirectoryError(f"--src 不是目录: {src_dir}")

    # 目标根目录允许不存在，自动建；已存在则复用
    dest_root.mkdir(parents=True, exist_ok=True)
    new_dir_name = _next_data_dir_name(dest_root, width=args.width)
    new_dir = dest_root / new_dir_name

    # 理论上 _next_data_dir_name 给的一定是不存在的；这层断言是"以防序号扫描有 bug"
    if new_dir.exists():
        raise FileExistsError(f"目标目录已存在（请检查序号扫描逻辑/目录内容）: {new_dir}")

    _copy_contents(src_dir, new_dir)
    # 打印新建目录的绝对路径；shell 管道里可以直接 `new=$(python collect_debug_data.py --dest-root ...)`
    print(str(new_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
