#!/usr/bin/env python
# -*- coding: utf-8 -*-
import zipfile
import xml.etree.ElementTree as ET

z = zipfile.ZipFile('packs/Shakhsvoyak.siq')
content = z.read('content.xml').decode('utf-8-sig')

root = ET.fromstring(content)
ns = '{http://vladimirkhil.com/ygpackage3.0.xsd}'

# Найдём тему "Сыграем в информатор"
for round_el in root.find('{0}rounds'.format(ns)).findall('{0}round'.format(ns)):
    for theme_el in round_el.find('{0}themes'.format(ns)).findall('{0}theme'.format(ns)):
        theme_name = theme_el.attrib.get('name', '')
        if 'информатор' in theme_name.lower():
            print("Theme:", theme_name)
            for q_el in theme_el.find('{0}questions'.format(ns)).findall('{0}question'.format(ns)):
                price = q_el.attrib.get('price', '?')
                print(f"\n  Question price={price}")
                scenario = q_el.find('{0}scenario'.format(ns))
                if scenario is not None:
                    for atom in scenario.findall('{0}atom'.format(ns)):
                        atom_type = atom.attrib.get('type', 'none')
                        atom_text = atom.text if atom.text else None
                        print(f"    atom type={atom_type}, text={repr(atom_text)}")
