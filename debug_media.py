#!/usr/bin/env python
# -*- coding: utf-8 -*-
import zipfile
import os
import tempfile
import shutil
import urllib.parse

z = zipfile.ZipFile('packs/Shakhsvoyak.siq')

# Извлекаем во временную папку
temp_dir = tempfile.mkdtemp()
print("Temp dir:", temp_dir)

try:
    z.extractall(temp_dir)
    
    # Проверяем файлы изображений
    print("\n=== Файлы в Images/ ===")
    img_dir = os.path.join(temp_dir, 'Images')
    if os.path.exists(img_dir):
        files = os.listdir(img_dir)
        print(f"Found {len(files)} files:")
        for f in files[:10]:
            print(f"  {f}")
    
    # Проверяем конкретный файл
    print("\n=== Проверка 'Контригра.png' ===")
    variants = [
        'Контригра.png',
        urllib.parse.quote('Контригра.png'),
        '%D0%9A%D0%BE%D0%BD%D1%82%D1%80%D0%B8%D0%B3%D1%80%D0%B0.png',
    ]
    for v in variants:
        path1 = os.path.join(temp_dir, 'Images', v)
        path2 = os.path.join(temp_dir, 'images', v)
        print(f"  Checking: Images/{v} -> {os.path.exists(path1)}")
        print(f"  Checking: images/{v} -> {os.path.exists(path2)}")
        
finally:
    shutil.rmtree(temp_dir, ignore_errors=True)
