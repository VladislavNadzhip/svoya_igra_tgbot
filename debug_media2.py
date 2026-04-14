#!/usr/bin/env python
# -*- coding: utf-8 -*-
import urllib.parse

# Тестируем кодирование
name = 'Контригра.png'
encoded = urllib.parse.quote(name, safe='')
print(f"Original: {name}")
print(f"Encoded: {encoded}")

# Тестируем с пробелом
name2 = 'един ход.png'
encoded2 = urllib.parse.quote(name2, safe='')
print(f"\nOriginal: {name2}")
print(f"Encoded: {encoded2}")

# Проверяем варианты в архиве
print("\n=== Варианты для 'Контригра.png' ===")
print(f"  clean: {name}")
print(f"  decoded: {urllib.parse.unquote(name)}")
print(f"  encoded: {encoded}")
