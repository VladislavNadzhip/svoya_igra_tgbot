#!/usr/bin/env python
# -*- coding: utf-8 -*-
import urllib.parse

name = '#2 3.PNG'
print('safe=#()-.:', urllib.parse.quote(name, safe='#()-._'))

name2 = 'Владимир Высоцкий - Честь шахматной короны (mp3cut.net).mp3'
print('audio:', urllib.parse.quote(name2, safe='#()-._'))

name3 = 'mashina-vremeni-shahmaty_(Krolik (mp3cut.net).mp3'
print('audio2:', urllib.parse.quote(name3, safe='#()-._'))

# Проверяем что в архиве
print("\n=== В архиве ===")
print("#2 3.PNG -> #2%203.PNG")
print("Vladimir... -> %D0%92%D0%BB... (полный url-encoding)")
