#!/usr/bin/env python
# -*- coding: utf-8 -*-
import zipfile
import xml.etree.ElementTree as ET
from siq_parser import parse_siq

# Сначала проверим что парсит парсер
pack = parse_siq('packs/Shakhsvoyak.siq')
print("=== ПАРСЕР ===")
print("Rounds:", len(pack.rounds))

empty_questions = []
total_questions = 0

for r_idx, r in enumerate(pack.rounds):
    print(f"\nRound {r_idx}: {r.name}")
    for t_idx, t in enumerate(r.themes):
        print(f"  Theme {t_idx}: {t.name} ({len(t.questions)} questions)")
        for q_idx, q in enumerate(t.questions):
            total_questions += 1
            if q.text == '(empty question)':
                empty_questions.append((r.name, t.name, q.price))
                print(f"    Q{q_idx} price={q.price}: *** EMPTY ***")
            else:
                print(f"    Q{q_idx} price={q.price}: {q.text[:60]}...")

print(f"\n\nИТОГО: {total_questions} вопросов, {len(empty_questions)} пустых")
if empty_questions:
    print("Пустые вопросы:")
    for r_name, t_name, price in empty_questions:
        print(f"  - {r_name} / {t_name} / {price}")
