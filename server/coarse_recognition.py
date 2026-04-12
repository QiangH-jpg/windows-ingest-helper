#!/usr/bin/env python3
"""
服务端粗识别入口
功能：读取 manifest.json → 粗分素材类型 → 专访分流 → 输出粗识别 JSON

运行方式：
    python coarse_recognition.py --manifest ./output/manifest.json

输出：
    coarse_recognition.json
"""
import os
import sys
import json
import argparse
from datetime import datetime

# 素材类型分类
CATEGORIES = {
    'activity': '活动画面',
    'service': '服务动作',
    'interaction': '互动镜头',
    'group_photo': '合影/横幅',
    'interview': '专访/讲话',
    'empty': '空镜/弱价值镜头'
}

def analyze_video_simple(proxy_path):
    """
    简单粗识别（本地规则）
    当前阶段只做硬过滤，不做最终编辑判断
    """
    # TODO: 接入 FFprobe 分析
    # 当前返回默认值
    return {
        'coarse_category': 'activity',  # 默认活动画面
        'is_interview': False,
        'is_low_value': False,
        'confidence': 0.5
    }

def coarse_recognition(manifest_path):
    """执行粗识别"""
    print(f"\n🔍 读取清单：{manifest_path}")
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    
    output_dir = os.path.dirname(manifest_path)
    coarse_result = {
        'version': '1.0',
        'created_at': datetime.now().isoformat(),
        'source_manifest': manifest_path,
        'total_files': len(manifest['processed_files']),
        'recognized_files': [],
        'interview_pool': [],
        'low_value_pool': [],
        'summary': {}
    }
    
    # 处理每个文件
    for i, item in enumerate(manifest['processed_files']):
        print(f"\n[{i+1}/{len(manifest['processed_files'])}] {item['proxy_filename']}")
        
        # 1. 简单分析
        analysis = analyze_video_simple(item['proxy_path'])
        
        # 2. 记录结果
        result = {
            'index': i,
            'original_path': item['original_path'],
            'proxy_path': item['proxy_path'],
            'tos_url': item.get('tos_url'),
            'duration': item['original_info'].get('duration', 0),
            'width': item['original_info'].get('width', 0),
            'height': item['original_info'].get('height', 0),
            'file_hash': item.get('file_hash'),
            'coarse_category': analysis['coarse_category'],
            'is_interview': analysis['is_interview'],
            'is_low_value': analysis['is_low_value'],
            'duplicate_group': None,  # 待聚类后填写
            'ingest_status': 'recognized',
            'preprocess_notes': item.get('preprocess_notes', []),
            'recognition_confidence': analysis['confidence']
        }
        
        coarse_result['recognized_files'].append(result)
        
        # 3. 分流
        if analysis['is_interview']:
            coarse_result['interview_pool'].append(result)
            print(f"  → 专访分流")
        elif analysis['is_low_value']:
            coarse_result['low_value_pool'].append(result)
            print(f"  → 低价值分流")
        else:
            print(f"  → {CATEGORIES.get(analysis['coarse_category'], '未知')}")
    
    # 4. 统计摘要
    category_count = {}
    for item in coarse_result['recognized_files']:
        cat = item['coarse_category']
        category_count[cat] = category_count.get(cat, 0) + 1
    
    coarse_result['summary'] = {
        'total': len(coarse_result['recognized_files']),
        'interview': len(coarse_result['interview_pool']),
        'low_value': len(coarse_result['low_value_pool']),
        'by_category': category_count,
        'recognition_time': datetime.now().isoformat()
    }
    
    # 5. 保存结果
    output_path = os.path.join(output_dir, 'coarse_recognition.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(coarse_result, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 粗识别完成")
    print(f"  总计：{coarse_result['summary']['total']} 个")
    print(f"  专访分流：{coarse_result['summary']['interview']} 个")
    print(f"  低价值分流：{coarse_result['summary']['low_value']} 个")
    print(f"  分类统计：{category_count}")
    print(f"  输出：{output_path}")
    
    return coarse_result

def main():
    parser = argparse.ArgumentParser(description='服务端粗识别入口')
    parser.add_argument('--manifest', required=True, help='manifest.json 路径')
    args = parser.parse_args()
    
    print("=" * 60)
    print("服务端粗识别入口 v1.0")
    print("=" * 60)
    
    coarse_recognition(args.manifest)
    
    print("\n" + "=" * 60)
    print("下一步：候选收缩")
    print("命令：python candidate_shrink.py --coarse ./output/coarse_recognition.json")
    print("=" * 60)

if __name__ == '__main__':
    main()
