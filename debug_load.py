#!/usr/bin/env python
# -*- coding: utf-8 -*-
import zipfile
import tempfile
import shutil
import os
import urllib.parse
from siq_parser import parse_siq, _load_media

# Извлекаем архив
temp_dir = tempfile.mkdtemp()
try:
    with zipfile.ZipFile('packs/Shakhsvoyak.siq', 'r') as zf:
        zf.extractall(temp_dir)
    
    # Проверяем файлы
    print("=== Проверка _load_media ===")
    
    test_cases = [
        ('@Контригра.png', 'Images'),
        ('@#2 3.PNG', 'Images'),
        ('@Владимир Высоцкий - Честь шахматной короны (mp3cut.net).mp3', 'Audio'),
        ('@vasily_smyslov_operatic_arias_1997_2151226526669108402 (mp3cut.net).mp3', 'Audio'),
    ]
    
    for ref, folder in test_cases:
        clean = ref.lstrip('@').strip()
        data, fname = _load_media(ref, folder, temp_dir)
        print(f"\n  ref: {ref}")
        print(f"  folder: {folder}")
        print(f"  result: data_len={len(data) if data else 0}, fname={fname}")
        
finally:
    shutil.rmtree(temp_dir, ignore_errors=True)
