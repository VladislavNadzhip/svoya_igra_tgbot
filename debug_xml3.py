#!/usr/bin/env python
# -*- coding: utf-8 -*-
import zipfile
import xml.etree.ElementTree as ET

z = zipfile.ZipFile('packs/Shakhsvoyak.siq')
content = z.read('content.xml').decode('utf-8-sig')

root = ET.fromstring(content)
ns = '{http://vladimirkhil.com/ygpackage3.0.xsd}'

# Проверяем проблемные темы
problem_themes = ['информатор', 'двухход', 'мелодию', 'молодёж', 'синем']

for round_el in root.find('{0}rounds'.format(ns)).findall('{0}round'.format(ns)):
    round_name = round_el.attrib.get('name', '')
    for theme_el in round_el.find('{0}themes'.format(ns)).findall('{0}theme'.format(ns)):
        theme_name = theme_el.attrib.get('name', '')
        
        if any(pt in theme_name.lower() for pt in problem_themes):
            print(f"\n=== Round: {round_name} / Theme: {theme_name} ===")
            
            for q_el in theme_el.find('{0}questions'.format(ns)).findall('{0}question'.format(ns)):
                price = q_el.attrib.get('price', '?')
                print(f"\n  Q price={price}")
                
                scenario = q_el.find('{0}scenario'.format(ns))
                if scenario is not None:
                    for atom in scenario.findall('{0}atom'.format(ns)):
                        atom_type = atom.attrib.get('type', 'none')
                        atom_text = atom.text if atom.text else None
                        print(f"    atom type={atom_type}, text={repr(atom_text)}")
                
                # Проверяем <type>
                type_el = q_el.find('{0}type'.format(ns))
                if type_el is not None:
                    type_name = type_el.attrib.get('name', '')
                    print(f"    type attr: {type_name}")
