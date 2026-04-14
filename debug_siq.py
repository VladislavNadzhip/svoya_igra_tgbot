#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Диагностика: показывает содержимое .siq-архива и что парсится в вопросах.
Запуск: python debug_siq.py <путь_к_файлу.siq>
"""
import sys
import zipfile
import urllib.parse
from siq_parser import parse_siq, get_pack_info

if len(sys.argv) < 2:
    print("Usage: python debug_siq.py <file.siq>")
    sys.exit(1)

path = sys.argv[1]

print("=" * 60)
print("ZIP CONTENTS")
print("=" * 60)
with zipfile.ZipFile(path, 'r') as zf:
    for name in sorted(zf.namelist()):
        info = zf.getinfo(name)
        decoded = urllib.parse.unquote(name)
        print(f"  [{info.file_size:>8} bytes]  {name}")
        if decoded != name:
            print(f"               decoded -> {decoded}")

print()
print("=" * 60)
print("PARSED PACK")
print("=" * 60)
try:
    pack = parse_siq(path)
    print(get_pack_info(pack))
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

print()
print("=" * 60)
print("QUESTIONS WITH MEDIA")
print("=" * 60)
for r in pack.rounds:
    for t in r.themes:
        for q in t.questions:
            has_media = q.image or q.audio or q.video
            print(f"  [{r.name}] {t.name} / {q.price}")
            print(f"    text   : {repr(q.text[:80]) if q.text else '(empty)'}")
            print(f"    answer : {q.answer}")
            if q.image:
                print(f"    image  : {q.image_filename} ({len(q.image)} bytes)")
            if q.audio:
                print(f"    audio  : {q.audio_filename} ({len(q.audio)} bytes)")
            if q.video:
                print(f"    video  : {q.video_filename} ({len(q.video)} bytes)")
            if not has_media and not q.text:
                print(f"    *** NO TEXT AND NO MEDIA ***")
            print()
