      
import os
import cv2
import matplotlib.pyplot as plt
import numpy as np
from collections import defaultdict

def preprocess_image(image_path, denoise=True, dilate=True):
    """读取图片并进行预处理：灰度化 -> 大津法二值化 -> (可选)去噪 -> (可选)膨胀"""
    img = cv2.imread(image_path)
    if img is None:
        return None, None, None, None
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # 大津法二值化
    ret, binary_img = cv2.threshold(gray_img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    # 去噪：使用中值滤波
    if denoise:
        binary_img = cv2.medianBlur(binary_img, 3)
    # plt.imshow(binary_img, cmap='gray')
    # plt.show()
    # 膨胀/腐蚀：针对黑色线条（0），使用erode操作来达到“加粗”效果
    if dilate:
        kernel = np.ones((3, 3), np.uint8)
        # 在二值图像中，0是黑线，255是白背景。cv2.erode 会扩张0，缩小255，从而加粗黑线。
        binary_img = cv2.erode(binary_img, kernel, iterations=1)
        
    return img_rgb, gray_img, binary_img, ret

def is_black(binary, y, x):
    if 0 <= y < binary.shape[0] and 0 <= x < binary.shape[1]:
        return binary[y, x] == 0
    return False

def check_robust_black(binary, x, y, count=3):
    consecutive = 0
    for dy in range(-count // 2, count // 2 + count):
        cy = y + dy
        if is_black(binary, cy, x):
            consecutive += 1
            if consecutive >= count:
                return True
        else:
            consecutive = 0
    return False

def trace_line_8neighbor(binary, start_x, start_y, y_min, y_max, line_id):
    """
    从起始点通过正交邻域追踪线条，仅向右生长。
    line_id: 'a' 或 'b'，用于决定y=1024的截止逻辑。
    """
    visited = set()
    queue = [(start_x, start_y)]
    visited.add((start_x, start_y))
    max_x = start_x
    max_x_y = start_y
    
    # 仅允许：向右(1,0), 向上(0,-1), 向下(0,1)
    ortho_dirs = [(1, 0), (0, -1), (0, 1)]
    # 用于斜率检测的对角方向
    diag_dirs = [(1, 1), (1, -1)]

    while queue:
        cx, cy = queue.pop(0)
        
        if cx > max_x:
            max_x = cx
            max_x_y = cy

        # 检测斜率：
        # 如果对角方向 (cx+1, cy+dy) 是黑点，但两个正交邻居 (cx+1, cy) 和 (cx, cy+dy) 都是白点，
        # 则说明这一步只能跳跃连接，属于“奇怪斜率”。
        for dx, dy in diag_dirs:
            nx, ny = cx + dx, cy + dy
            if is_black(binary, ny, nx):
                if not is_black(binary, cy, cx + 1) and not is_black(binary, ny, cx):
                    print(f"[Warning] Strange slope detected at ({cx}, {cy}) towards ({nx}, {ny}). Stopping trace for line {line_id}.")
                    return max_x, max_x_y, visited

        for dx, dy in ortho_dirs:
            nx, ny = cx + dx, cy + dy
            
            # 边界检查
            if not (y_min <= ny <= y_max):
                continue
            
            # y=1024 截止限制
            if line_id == 'a' and ny > 1024:
                continue
            if line_id == 'b' and ny < 1024:
                continue

            if (nx, ny) not in visited and 0 <= nx < binary.shape[1]:
                if is_black(binary, ny, nx):
                    visited.add((nx, ny))
                    queue.append((nx, ny))
                    
    return max_x, max_x_y, visited

def find_turning_point(pixels, start_y):
    columns = defaultdict(list)
    for px, py in pixels:
        columns[px].append(py)
    if not columns:
        return None
    col_stats = {x: {'span': max(ys) - min(ys)} for x, ys in columns.items()}
    sorted_xs = sorted(col_stats.keys())
    if len(sorted_xs) < 20:
        return None
    baseline_count = min(50, len(sorted_xs) // 4)
    baseline_spans = [col_stats[x]['span'] for x in sorted_xs[:baseline_count]]
    avg_span = sum(baseline_spans) / len(baseline_spans)
    threshold = max(avg_span * 3, 10)
    for x in sorted_xs[baseline_count:]:
        if col_stats[x]['span'] > threshold:
            idx = sorted_xs.index(x)
            if all(col_stats[sorted_xs[i]]['span'] > threshold for i in range(idx, min(idx + 5, len(sorted_xs)))):
                return x
    return None

def analyze_calibration(binary_img, x_start, y_top, y_bottom, x_step, noise_threshold):
    """分析校准图，获取关键坐标"""
    h, w = binary_img.shape
    
    # 找线a (上往下)
    found_a, a_x, a_y = False, -1, -1
    for x in range(x_start, w, x_step):
        for y in range(y_top, y_bottom + 1):
            if is_black(binary_img, y, x) and check_robust_black(binary_img, x, y, noise_threshold):
                found_a, a_x, a_y = True, x, y
                break
        if found_a: break
    
    a_end_x, a_end_y, a_pixels = (None, None, set())
    if found_a:
        a_end_x, a_end_y, a_pixels = trace_line_8neighbor(binary_img, a_x, a_y, y_top, y_bottom, 'a')

    # 找线b (下往上)
    found_b, b_x, b_y = False, -1, -1
    for x in range(x_start, w, x_step):
        for y in range(y_bottom, y_top - 1, -1):
            if is_black(binary_img, y, x) and check_robust_black(binary_img, x, y, noise_threshold):
                found_b, b_x, b_y = True, x, y
                break
        if found_b: break
    
    b_end_x, b_end_y, b_pixels = (None, None, set())
    if found_b:
        b_end_x, b_end_y, b_pixels = trace_line_8neighbor(binary_img, b_x, b_y, y_top, y_bottom, 'b')

    # 拐点
    tp_a = find_turning_point(a_pixels, a_y) if found_a else None
    tp_b = find_turning_point(b_pixels, b_y) if found_b else None
    
    scale_line_x = max([t for t in [tp_a, tp_b] if t is not None]) if (tp_a or tp_b) else None
    detection_start_x = min([x for x in [a_end_x, b_end_x] if x is not None]) if (a_end_x or b_end_x) else None
    
    return {
        'scale_line_x': scale_line_x,
        'detection_start_x': detection_start_x,
        'y_a': a_y,
        'y_b': b_y,
        'a_start': (a_x, a_y),
        'a_end': (a_end_x, a_end_y),
        'b_start': (b_x, b_y),
        'b_end': (b_end_x, b_end_y),
        'tp_a': tp_a,
        'tp_b': tp_b,
        'a_pixels': a_pixels,
        'b_pixels': b_pixels
    }

def main():
    folder_path = '/home/aaa/test/image_check/session_20000104_032543'
    output_dir = os.path.join(folder_path, 'output_results')
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    image_files = sorted([f for f in os.listdir(folder_path) if f.endswith(('.png', '.jpg', '.jpeg'))])
    if len(image_files) < 1:
        print("图片路径中没有图片。")
        return

    params = {'x_start': 800, 'y_top': 512, 'y_bottom': 1536, 'x_step': 5, 'noise_threshold': 3}

    # 步骤1: 校准 (使用第0张图进行一次性校准)
    print(f"[Calibration] Using {image_files[0]}")
    rgb0, gray0, bin0, otsu_ret = preprocess_image(os.path.join(folder_path, image_files[0]), denoise=True, dilate=True)
    res = analyze_calibration(bin0, **params)
    
    # 打印一次颜色说明
    print(f"\n大津法计算得到的阈值为: {otsu_ret}")
    print("\n[可视化说明]：")
    print("  - 绿色垂直线 (Green): 刻度线 (scale_line)")
    print("  - 品红垂直线 (Magenta): 检测起点 (detection_start)")
    print("  - 橙色垂直线 (Orange): 检测到的液面线 (liquid_level)")
    print("  - 红色像素点 (Red): 追踪到的液面区域")
    print("  - 蓝色/青色区域: 追踪到的校准参考线 a/b")
    print("  - 红色细水平线: 搜索范围 y 轴边界\n")

    # 步骤2: 实时批量检测
    liquid_entered = False
    for i, img_name in enumerate(image_files):
        print(f"[{i+1}/{len(image_files)}] Processing {img_name}...")
        img_path = os.path.join(folder_path, img_name)
        rgb_curr, _, bin_curr, _ = preprocess_image(img_path)
        
        liquid_max_x = None
        current_status_msg = ""
        
        if res['detection_start_x'] is not None:
            scan_y = 1024
            found_liquid_start = False
            start_lx = -1
            
            # 从检测线左侧 1 像素开始向左扫描
            # 理由：检测线右侧的都不考虑
            for x in range(res['detection_start_x'] - 1, 0, -1):
                if is_black(bin_curr, scan_y, x):
                    if check_robust_black(bin_curr, x, scan_y, 2):
                        found_liquid_start = True
                        start_lx = x
                        break
            
            if found_liquid_start:
                # 追踪液面
                l_max_x, _, _ = trace_line_8neighbor(
                    bin_curr, start_lx, scan_y, res['y_a'], res['y_b'], 'liquid'
                )
                
                # 约束：追踪到的最大x不能超过检测线
                if l_max_x >= res['detection_start_x']:
                    l_max_x = start_lx # 如果追踪越界，保守取起始点
                
                # 进入区域逻辑：只有液面第一次出现在检测线左侧 100-200 像素内才激活
                if not liquid_entered:
                    if (res['detection_start_x'] - l_max_x) <= 200 and (res['detection_start_x'] - l_max_x) >= 100:
                        liquid_entered = True
                        print("  [System] 液面已进入检测区域")
                
                if liquid_entered:
                    liquid_max_x = l_max_x
                    # 到达刻度线判断
                    if res['scale_line_x'] is not None:
                        dist = abs(liquid_max_x - res['scale_line_x'])
                        if dist <= 30:
                            current_status_msg = "Liquid level reaches the scale"
                        else:
                            current_status_msg = "Not reaching the scale"
                    
                    print(f"  [liquid_level] x={liquid_max_x} ({current_status_msg})")
                else:
                    print("  [liquid_level] 扫描到的液面不真实（未进入进入100-200px区域），忽略")
            else:
                print("  [liquid_level] 未检测到起始点")

        # 可视化并保存
        plt.figure(figsize=(20, 10))
        
        # 子图1: 校准基准图预览
        ax1 = plt.subplot(2, 2, 1)
        res_img0 = rgb0.copy()
        for px, py in res['a_pixels']: res_img0[py, px] = [0, 0, 255]
        for px, py in res['b_pixels']: res_img0[py, px] = [0, 255, 255]
        if res['scale_line_x']: cv2.line(res_img0, (res['scale_line_x'], params['y_top']), (res['scale_line_x'], params['y_bottom']), (0, 255, 0), 3)
        if res['detection_start_x']: cv2.line(res_img0, (res['detection_start_x'], params['y_top']), (res['detection_start_x'], params['y_bottom']), (255, 0, 255), 3)
        ax1.imshow(res_img0)
        ax1.set_title(f"Calibration Reference\nScale Line: {res['scale_line_x']}, Start: {res['detection_start_x']}")
        ax1.axis('off')

        # 子图2: 当前帧二值化
        ax2 = plt.subplot(2, 2, 2)
        ax2.imshow(bin0, cmap='gray')
        ax2.set_title(f"Start image Binary")
        ax2.axis('off')

        # 子图3: 当前帧检测结果
        ax3 = plt.subplot(2, 2, 3)
        res_img_curr = rgb_curr.copy()
            
        if res['scale_line_x']: cv2.line(res_img_curr, (res['scale_line_x'], params['y_top']), (res['scale_line_x'], params['y_bottom']), (0, 255, 0), 3)
        if res['detection_start_x']: cv2.line(res_img_curr, (res['detection_start_x'], params['y_top']), (res['detection_start_x'], params['y_bottom']), (255, 0, 255), 3)
        
        if liquid_max_x is not None:
            cv2.line(res_img_curr, (liquid_max_x, res['y_a']), (liquid_max_x, res['y_b']), (255, 165, 0), 3)
            
        if res['y_a']: cv2.line(res_img_curr, (0, res['y_a']), (rgb_curr.shape[1], res['y_a']), (255, 0, 0), 1)
        if res['y_b']: cv2.line(res_img_curr, (0, res['y_b']), (rgb_curr.shape[1], res['y_b']), (255, 0, 0), 1)
        
        ax3.imshow(res_img_curr)
        ax3.set_title(f"Detection Result: {current_status_msg}\nx={liquid_max_x if liquid_max_x else 'N/A'}")
        ax3.axis('off')

        # 子图4
        ax4 = plt.subplot(2, 2, 4)
        ax4.imshow(bin_curr, cmap='gray')
        ax2.set_title(f"Processed Binary: {img_name}")
        ax4.axis('off')

        plt.tight_layout()
        save_path = os.path.join(output_dir, f"res_{img_name}")
        plt.savefig(save_path, dpi=100)
        plt.close() # 释放内存

    print(f"\n全部图片处理完成，结果已保存至: {output_dir}")

if __name__ == "__main__":
    main()
    