#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
91Porn Spider - 重写版
修复内容：
1. 使用 cloudscraper 绕过 Cloudflare
2. strencode2 = unescape（URL解码），不再需要RC4
3. 支持代理
4. 多线程下载
5. 增量爬取 + doneDB 去重
"""

import requests
import cloudscraper
import re
import os
import sys
import time
import random
import threading
import subprocess
import urllib.parse
from urllib.parse import unquote
from collections import defaultdict

# ========== 配置 ==========
PROXY = {
    'http': 'http://192.168.10.6:7890',
    'https': 'http://192.168.10.6:7890',
}

BASE_URL = 'https://91porn.com'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Cookie': 'lg=zh-CN',
    'Referer': BASE_URL + '/',
}

# 视频存放目录
VIDEO_DIR = '/vol2/@apphome/trim.openclaw/data/91porn-spider-master/videos'
DONEDB_DIR = '/vol2/@apphome/trim.openclaw/data/91porn-spider-master/doneDB'

# 并发数
THREADS = 3

# ========== 工具函数 ==========

def make_session():
    """创建带代理的 cloudscraper session"""
    session = requests.Session()
    session.proxies = PROXY
    scraper = cloudscraper.create_scraper(sess=session)
    return scraper

def get_page(scraper, url, retry=3):
    """带重试的页面获取"""
    for i in range(retry):
        try:
            resp = scraper.get(url, headers=HEADERS, timeout=20)
            if len(resp.text) > 100:
                return resp.text
            # 空内容，age verification 页面
            if 'age' in resp.text.lower() or len(resp.text) < 1000:
                # 尝试带 session_language
                resp2 = scraper.get(url, headers={**HEADERS, 'session_language': 'cn_CN'}, timeout=20)
                if len(resp2.text) > 100:
                    return resp2.text
        except Exception as e:
            print(f'  [!] 获取失败 ({i+1}/{retry}): {e}')
            time.sleep(2)
    return None

def extract_viewkeys(html):
    """从列表页提取 viewkey"""
    if not html:
        return []
    # viewkey 可以是 viewkey=xxx 或 viewkey%3Dxxx
    keys = re.findall(r'viewkey=([a-zA-Z0-9]{10,})', html)
    return list(set(keys))

def extract_video_url(html):
    """从视频页提取真实视频URL
    1. 尝试 strencode2 解码（unescape/URL decode）
    2. 尝试直接 <source> 标签
    """
    if not html:
        return None, None

    # 方法1: strencode2("...") -> URL decode
    encoded_matches = re.findall(r'strencode2\("([^"]+)"\)', html)
    for encoded in encoded_matches:
        try:
            decoded = unquote(unquote(encoded))  # double decode
            src_match = re.search(r"src=['\"]([^'\"]+)['\"]", decoded)
            if src_match:
                url = src_match.group(1)
                # 提取域名
                domain = re.search(r'https?://([^/]+)', url)
                return url, domain.group(1) if domain else 'unknown'
        except Exception as e:
            print(f'  [!] strencode2 解码失败: {e}')
            continue

    # 方法2: 直接 <source> 标签（有些视频页直接有）
    src_matches = re.findall(r'<source\s+src=["\']([^"\']+)["\']', html)
    for url in src_matches:
        if '.mp4' in url or '.m3u8' in url:
            domain = re.search(r'https?://([^/]+)', url)
            return url, domain.group(1) if domain else 'unknown'

    return None, None

def extract_title(html):
    """提取视频标题"""
    if not html:
        return None
    # 从 title 提取（最准确）
    match = re.search(r'<title>\s*([^\n<]+?)\s*- 91porn', html)
    if match:
        return match.group(1).strip()[:200]
    # 备选：直接 title 标签
    match = re.search(r'<title>([^<]+)</title>', html)
    if match:
        title = match.group(1).replace(' - 91porn', '').strip()
        if title and len(title) > 3:
            return title[:200]
    return None

def safe_filename(name):
    """生成安全文件名"""
    if not name:
        return 'untitled'
    # 移除非法字符
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name[:150].strip()
    return name or 'untitled'

def download_video(url, filepath, referer=BASE_URL, retry=2):
    """用 curl 下载视频（支持大文件）"""
    for attempt in range(retry):
        try:
            cmd = [
                'curl', '-L', '--max-time', '600',
                '-o', filepath,
                '-A', HEADERS['User-Agent'],
                '-H', f'Referer: {referer}',
                '-x', PROXY['http'],
                url
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=620)
            if result.returncode == 0 and os.path.getsize(filepath) > 1024*1024:
                return True
            elif os.path.exists(filepath):
                size = os.path.getsize(filepath)
                if size > 1024*1024:
                    return True
                os.remove(filepath)
        except Exception as e:
            print(f'  [!] 下载失败 (attempt {attempt+1}): {e}')
            time.sleep(3)
    return False

def load_donedb(cat):
    """加载 doneDB"""
    db_file = os.path.join(DONEDB_DIR, f'doneDB_{cat}')
    if os.path.exists(db_file):
        with open(db_file, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_donedb(cat, viewkeys):
    """追加保存 doneDB"""
    os.makedirs(DONEDB_DIR, exist_ok=True)
    db_file = os.path.join(DONEDB_DIR, f'doneDB_{cat}')
    with open(db_file, 'a', encoding='utf-8') as f:
        for vk in sorted(viewkeys):
            f.write(vk + '\n')

# ========== 爬虫核心 ==========

def crawl_category(category='top', pages=3, incremental=True):
    """爬取指定分类
    category: top/rf/md/mf/ori
    pages: 爬取页数
    incremental: True=增量（跳过doneDB已有的）False=全量
    """
    cat_names = {
        'top': ('top', '每月最热'),
        'rf': ('rf', '加精'),
        'md': ('md', '本月讨论'),
        'mf': ('mf', '本月收藏'),
        'ori': ('ori', '原创'),
    }
    cat_key, cat_display = cat_names.get(category, (category, category))

    print(f'\n========== 开始爬取: {cat_display} (category={cat_key}) ==========')

    scraper = make_session()

    # 列表页URL
    list_url = f'{BASE_URL}/v.php?category={cat_key}&viewtype=basic&page='

    all_viewkeys = []
    for page in range(1, pages + 1):
        print(f'\n--- 第 {page}/{pages} 页 ---')
        url = list_url + str(page)
        html = get_page(scraper, url)
        if not html:
            print(f'  [!] 获取列表页失败')
            continue

        viewkeys = extract_viewkeys(html)
        print(f'  找到 {len(viewkeys)} 个视频')
        all_viewkeys.extend(viewkeys)

        # 避免请求过快
        time.sleep(random.uniform(1, 2))

    all_viewkeys = list(set(all_viewkeys))
    print(f'\n共 {len(all_viewkeys)} 个视频待处理')

    # 增量过滤
    if incremental:
        done_db = load_donedb(cat_key)
        new_keys = [vk for vk in all_viewkeys if vk not in done_db]
        print(f'增量模式: 跳过 {len(all_viewkeys)-len(new_keys)} 个已有, 剩余 {len(new_keys)} 个')
        all_viewkeys = new_keys
    else:
        print(f'全量模式: 全部 {len(all_viewkeys)} 个')

    if not all_viewkeys:
        print('没有新视频')
        return

    # 多线程下载
    queue = defaultdict(list)  # domain -> list of (viewkey, url, title)
    done_this_run = []

    print(f'\n开始解析视频URL ({THREADS} 线程)...')

    def resolve_video(vk):
        video_url = f'{BASE_URL}/view_video.php?viewkey={vk}'
        html = get_page(scraper, video_url)
        if not html:
            return None
        url, domain = extract_video_url(html)
        title = extract_title(html)
        return vk, url, domain, title

    # 解析所有视频URL
    for vk in all_viewkeys:
        result = resolve_video(vk)
        if result and result[1]:
            vk, url, domain, title = result
            queue[domain].append((vk, url, title))
            print(f'  [OK] {title[:50] if title else vk} -> {domain}')
        else:
            print(f'  [FAIL] viewkey={vk}')
        time.sleep(random.uniform(0.5, 1.5))

    # 按 domain 分组下载
    new_viewkeys = [vk for vk, url, title in [item for items in queue.values() for item in items]]
    save_donedb(cat_key, new_viewkeys)

    total = sum(len(v) for v in queue.values())
    print(f'\n解析完成: {total} 个视频可下载，分布在 {len(queue)} 个CDN')

    for domain, items in queue.items():
        print(f'\nCDN: {domain} ({len(items)} 个)')
        for vk, url, title in items:
            filename = safe_filename(title) + '_' + vk + '.mp4'
            filepath = os.path.join(VIDEO_DIR, filename)

            if os.path.exists(filepath) and os.path.getsize(filepath) > 1024*1024:
                print(f'  [EXISTS] {title[:40]} (跳过)')
                continue

            print(f'  [DOWN] {title[:50] if title else vk}...', end=' ', flush=True)
            ok = download_video(url, filepath)
            if ok:
                size = os.path.getsize(filepath)
                print(f'OK ({size//1024//1024}MB)')
            else:
                print('FAIL')
            time.sleep(random.uniform(1, 3))

    print(f'\n========== {cat_display} 完成 ==========')

# ========== 命令行入口 ==========

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='91Porn Spider')
    parser.add_argument('category', nargs='?', default='top',
                        choices=['top', 'rf', 'md', 'mf', 'ori'],
                        help='分类: top/rf/md/mf/ori')
    parser.add_argument('-p', '--pages', type=int, default=3, help='爬取页数')
    parser.add_argument('--full', action='store_true', help='全量（禁用增量）')
    args = parser.parse_args()

    os.makedirs(VIDEO_DIR, exist_ok=True)
    os.makedirs(DONEDB_DIR, exist_ok=True)

    crawl_category(args.category, pages=args.pages, incremental=not args.full)
