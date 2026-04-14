#!/usr/bin/env python
# -*- coding: utf-8 -*-
import urllib.parse
import zipfile

z = zipfile.ZipFile('packs/Shakhsvoyak.siq')

# Точное сравнение
name = 'Владимир Высоцкий - Честь шахматной короны (mp3cut.net).mp3'
encoded = urllib.parse.quote(name, safe='#()-._')
archive_name = '%D0%92%D0%BB%D0%B0%D0%B4%D0%B8%D0%BC%D0%B8%D1%80%20%D0%92%D1%8B%D1%81%D0%BE%D1%86%D0%BA%D0%B8%D0%B9%20-%20%D0%A7%D0%B5%D1%81%D1%82%D1%8C%20%D1%88%D0%B0%D1%85%D0%BC%D0%B0%D1%82%D0%BD%D0%BE%D0%B9%20%D0%BA%D0%BE%D1%80%D0%BE%D0%BD%D1%8B%20(mp3cut.net).mp3'

print("Encoded:  ", repr(encoded))
print("Archive:  ", repr(archive_name))
print("Match:     ", encoded == archive_name)

# Проверяем есть ли файл в архиве
full_path = 'Audio/' + encoded
print("\nIn archive:", full_path in z.namelist())
print("Archive names starting with Audio/V:")
for n in z.namelist():
    if n.startswith('Audio/%D0%92'):
        print("  ", n)
