#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Диагностика: показывает содержимое .siq-архива, XML-атомы и поиск медиа.
Запуск: python debug_siq.py <путь_к_файлу.siq>
"""
import sys
import zipfile
import urllib.parse
import os
import xml.etree.ElementTree as ET

if len(sys.argv) < 2:
    print("Usage: python debug_siq.py <file.siq>")
    sys.exit(1)

path = sys.argv[1]

print("=" * 60)
print("ZIP CONTENTS (raw repr)")
print("=" * 60)
with zipfile.ZipFile(path, 'r') as zf:
    all_names = zf.namelist()
    for name in sorted(all_names):
        decoded = urllib.parse.unquote(name)
        size = zf.getinfo(name).file_size
        print(f"  {size:>8}B  raw={repr(name)}")
        if decoded != name:
            print(f"           dec={repr(decoded)}")

print()
print("=" * 60)
print("BUILDING INDEX")
print("=" * 60)
index = {}
def _add(key, value):
    if key and key not in index:
        index[key] = value

with zipfile.ZipFile(path, 'r') as zf:
    for zip_name in zf.namelist():
        _add(zip_name.lower(), zip_name)
        decoded_full = urllib.parse.unquote(zip_name).lower()
        _add(decoded_full, zip_name)
        base_raw = os.path.basename(zip_name).lower()
        _add(base_raw, zip_name)
        base_dec = urllib.parse.unquote(base_raw)
        _add(base_dec, zip_name)

print(f"Index size: {len(index)} keys")
print()

print("=" * 60)
print("XML ATOM / STEP ANALYSIS")
print("=" * 60)

with zipfile.ZipFile(path, 'r') as zf:
    content_xml = None
    for name in zf.namelist():
        if name.lower() == 'content.xml' or name.lower().endswith('/content.xml'):
            content_xml = zf.read(name).decode('utf-8-sig')
            break

    if not content_xml:
        print("ERROR: content.xml not found!")
        sys.exit(1)

    root = ET.fromstring(content_xml)
    ns = ''
    if root.tag.startswith('{'):
        ns = root.tag.split('}')[0] + '}'
    print(f"Namespace: {repr(ns)}")
    print()

    def try_find(resource_name, folder):
        clean = resource_name.lstrip('@').lstrip('/')
        decoded = urllib.parse.unquote(clean)
        base = os.path.basename(decoded)
        candidates = [
            f"{folder}/{decoded}",
            f"{folder.lower()}/{decoded}",
            f"{folder}/{clean}",
            f"{folder.lower()}/{clean}",
            decoded, clean, base,
        ]
        for c in candidates:
            if c.lower() in index:
                return c, index[c.lower()]
        return None, None

    for r_el in root.iter(f"{ns}round"):
        rname = r_el.attrib.get('name', '?')
        for t_el in r_el.iter(f"{ns}theme"):
            tname = t_el.attrib.get('name', '?')
            for q_el in t_el.findall(f"{ns}questions/{ns}question"):
                price = q_el.attrib.get('price', '?')

                # Old format
                scenario = q_el.find(f"{ns}scenario")
                if scenario is not None:
                    for atom in scenario.findall(f"{ns}atom"):
                        atype = atom.attrib.get('type', 'text').lower()
                        atext = (atom.text or '').strip()
                        if atype in ('image', 'audio', 'voice', 'video'):
                            folder = {'image': 'Images', 'audio': 'Audio', 'voice': 'Audio', 'video': 'Video'}.get(atype, 'Images')
                            cand, found = try_find(atext, folder)
                            status = f"OK -> {repr(found)}" if found else "*** NOT FOUND ***"
                            print(f"[{rname}] {tname} / {price}: atom type={atype}")
                            print(f"  raw text : {repr(atext)}")
                            print(f"  clean    : {repr(atext.lstrip('@').lstrip('/'))}")
                            print(f"  status   : {status}")
                            if not found:
                                base = os.path.basename(urllib.parse.unquote(atext.lstrip('@')))
                                similar = [k for k in index if base[:5].lower() in k]
                                print(f"  similar  : {similar[:8]}")
                            print()

                # New format
                body = q_el.find(f"{ns}body")
                if body is not None:
                    for step in body:
                        stext = (step.text or '').strip()
                        if stext.startswith('@'):
                            resource = stext[1:]
                            found_any = False
                            for folder in ('Images', 'Audio', 'Video', 'Sounds'):
                                cand, found = try_find(resource, folder)
                                if found:
                                    print(f"[{rname}] {tname} / {price}: step @resource")
                                    print(f"  raw      : {repr(stext)}")
                                    print(f"  status   : OK in {folder} -> {repr(found)}")
                                    print()
                                    found_any = True
                                    break
                            if not found_any:
                                print(f"[{rname}] {tname} / {price}: step @resource")
                                print(f"  raw      : {repr(stext)}")
                                print(f"  status   : *** NOT FOUND ***")
                                base = os.path.basename(urllib.parse.unquote(resource))
                                similar = [k for k in index if base[:5].lower() in k]
                                print(f"  similar  : {similar[:8]}")
                                print()
