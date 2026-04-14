#!/usr/bin/env python
# -*- coding: utf-8 -*-
import zipfile
import urllib.parse

z = zipfile.ZipFile('packs/Shakhsvoyak.siq')

# Ищем файлы #2 и аудио
print("=== Файлы #2 в Images ===")
for name in z.namelist():
    if '#2' in name:
        print(f"  {name}")

print("\n=== Файлы в Audio ===")
for name in z.namelist():
    if name.startswith('Audio/'):
        print(f"  {name}")

# Проверяем encoding для #2 3.PNG
print("\n=== Encoding тест ===")
name = '#2 3.PNG'
print(f"  Original: {name}")
print(f"  quote: {urllib.parse.quote(name, safe='')}")
print(f"  quote safe=' ': {urllib.parse.quote(name, safe=' ')}")

# Проверяем аудио
name2 = 'Владимир Высоцкий - Честь шахматной короны (mp3cut.net).mp3'
print(f"\n  Original: {name2}")
print(f"  quote: {urllib.parse.quote(name2, safe='')}")
