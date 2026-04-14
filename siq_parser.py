"""
Парсер пакетов SIGame (.siq файлы).
"""

import zipfile
import xml.etree.ElementTree as ET
import os
import tempfile
import shutil
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, Tuple, List


@dataclass
class Question:
    price: int
    text: str
    answer: str
    question_type: str = "standard"
    theme_comment: str = ""
    image: Optional[bytes] = None
    image_filename: Optional[str] = None
    audio: Optional[bytes] = None
    audio_filename: Optional[str] = None
    video: Optional[bytes] = None
    video_filename: Optional[str] = None


@dataclass
class Theme:
    name: str
    comment: str = ""
    questions: List[Question] = field(default_factory=list)


@dataclass
class Round:
    name: str
    round_type: str = "standard"
    themes: List[Theme] = field(default_factory=list)


@dataclass
class GamePack:
    name: str
    author: str = ""
    comment: str = ""
    rounds: List[Round] = field(default_factory=list)


def parse_siq(file_path: str) -> GamePack:
    if not zipfile.is_zipfile(file_path):
        raise ValueError("Файл не является валидным .siq (ZIP) архивом")

    temp_dir = tempfile.mkdtemp()

    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            zf.extractall(temp_dir)

        content_path = _find_content_xml(temp_dir)
        if content_path is None:
            raise ValueError("content.xml не найден в архиве")

        with open(content_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()

        root = ET.fromstring(content)

        ns = ''
        if root.tag.startswith('{'):
            ns = root.tag.split('}')[0] + '}'

        pack = _parse_pack(root, ns, temp_dir)
        return pack

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def get_pack_info(pack: GamePack) -> str:
    lines = []
    lines.append("Pack: {}".format(pack.name))
    if pack.author:
        lines.append("Author: {}".format(pack.author))
    if pack.comment:
        lines.append("{}".format(pack.comment))
    lines.append("")

    total_questions = 0
    for i, r in enumerate(pack.rounds, 1):
        q_count = sum(len(t.questions) for t in r.themes)
        total_questions += q_count
        rtype = " (final)" if r.round_type == 'final' else ""
        lines.append("Round {}: {}{}".format(i, r.name, rtype))
        lines.append("   Themes: {}, questions: {}".format(len(r.themes), q_count))

        for t in r.themes:
            prices = [str(q.price) for q in t.questions]
            lines.append("   - {} [{}]".format(t.name, ', '.join(prices)))

    lines.append("")
    lines.append("Total questions: {}".format(total_questions))
    return '\n'.join(lines)


def _find_content_xml(temp_dir: str) -> Optional[str]:
    direct = os.path.join(temp_dir, 'content.xml')
    if os.path.exists(direct):
        return direct

    for root_dir, dirs, files in os.walk(temp_dir):
        if 'content.xml' in files:
            return os.path.join(root_dir, 'content.xml')

    return None


def _parse_pack(root, ns: str, temp_dir: str) -> GamePack:
    pack_name = root.attrib.get('name', 'No name')
    pack_author = ''
    pack_comment = ''

    info_el = root.find('{0}info'.format(ns))
    if info_el is not None:
        authors_el = info_el.find('{0}authors'.format(ns))
        if authors_el is not None:
            author_els = authors_el.findall('{0}author'.format(ns))
            pack_author = ', '.join(a.text for a in author_els if a.text)

        comments_el = info_el.find('{0}comments'.format(ns))
        if comments_el is not None:
            if comments_el.text:
                pack_comment = comments_el.text

    pack = GamePack(name=pack_name, author=pack_author, comment=pack_comment)

    rounds_el = root.find('{0}rounds'.format(ns))
    if rounds_el is None:
        return pack

    for round_el in rounds_el.findall('{0}round'.format(ns)):
        game_round = _parse_round(round_el, ns, temp_dir)
        if game_round and game_round.themes:
            pack.rounds.append(game_round)

    return pack


def _parse_round(round_el, ns: str, temp_dir: str) -> Round:
    round_name = round_el.attrib.get('name', 'Round')
    raw_type = round_el.attrib.get('type', 'standard').lower()

    if raw_type == 'final':
        round_type = 'final'
    else:
        round_type = 'standard'

    game_round = Round(name=round_name, round_type=round_type)

    themes_el = round_el.find('{0}themes'.format(ns))
    if themes_el is None:
        return game_round

    for theme_el in themes_el.findall('{0}theme'.format(ns)):
        theme = _parse_theme(theme_el, ns, temp_dir)
        if theme and theme.questions:
            game_round.themes.append(theme)

    return game_round


def _parse_theme(theme_el, ns: str, temp_dir: str) -> Theme:
    theme_name = theme_el.attrib.get('name', 'Theme')

    comment = ''
    info_el = theme_el.find('{0}info'.format(ns))
    if info_el is not None:
        comments_el = info_el.find('{0}comments'.format(ns))
        if comments_el is not None:
            if comments_el.text:
                comment = comments_el.text.strip()

    theme = Theme(name=theme_name, comment=comment)

    questions_el = theme_el.find('{0}questions'.format(ns))
    if questions_el is None:
        return theme

    for question_el in questions_el.findall('{0}question'.format(ns)):
        question = _parse_question(question_el, ns, temp_dir)
        if question:
            theme.questions.append(question)

    return theme


def _parse_question(question_el, ns: str, temp_dir: str) -> Optional[Question]:
    price = 100
    try:
        price = int(question_el.attrib.get('price', 100))
    except (ValueError, TypeError):
        pass

    question_type = 'standard'
    type_el = question_el.find('{0}type'.format(ns))
    if type_el is not None:
        qtype = type_el.attrib.get('name', 'standard').lower()
        type_map = {
            'cat': 'cat',
            'bagcat': 'bagcat',
            'auction': 'auction',
            'sponsored': 'standard',
            'simple': 'standard',
            'stake': 'auction',
            'secret': 'cat',
            'secretnoq': 'cat',
        }
        question_type = type_map.get(qtype, 'standard')

    question_text = ''
    image_data = None
    image_filename = None
    audio_data = None
    audio_filename = None
    video_data = None
    video_filename = None

    scenario_el = question_el.find('{0}scenario'.format(ns))
    if scenario_el is not None:
        for atom_el in scenario_el.findall('{0}atom'.format(ns)):
            atom_type = atom_el.attrib.get('type', '').lower()
            atom_text = (atom_el.text or '').strip()

            if atom_type in ('', 'text', 'say'):
                if atom_text:
                    question_text = _append_text(question_text, atom_text)

            elif atom_type == 'image':
                if atom_text and image_data is None:
                    image_data, image_filename = _load_media(atom_text, 'Images', temp_dir)

            elif atom_type in ('voice', 'audio'):
                if atom_text and audio_data is None:
                    audio_data, audio_filename = _load_media(atom_text, 'Audio', temp_dir)

            elif atom_type == 'video':
                if atom_text and video_data is None:
                    video_data, video_filename = _load_media(atom_text, 'Video', temp_dir)

    body_el = question_el.find('{0}body'.format(ns))
    if body_el is not None:
        for step_el in body_el:
            tag = step_el.tag.replace(ns, '').lower()
            if tag != 'step':
                continue

            step_text = (step_el.text or '').strip()
            if not step_text:
                continue

            if step_text.startswith('@'):
                resource_name = step_text[1:]
                loaded = False

                data, fname = _load_media(resource_name, 'Images', temp_dir)
                if data and image_data is None:
                    image_data, image_filename = data, fname
                    loaded = True

                if not loaded:
                    data, fname = _load_media(resource_name, 'Audio', temp_dir)
                    if data and audio_data is None:
                        audio_data, audio_filename = data, fname
                        loaded = True

                if not loaded:
                    data, fname = _load_media(resource_name, 'Video', temp_dir)
                    if data and video_data is None:
                        video_data, video_filename = data, fname
                        loaded = True

                if not loaded:
                    question_text = _append_text(question_text, step_text)
            else:
                question_text = _append_text(question_text, step_text)

    answer_text = _parse_answer(question_el, ns)
    if not answer_text:
        answer_text = "(no answer)"

    return Question(
        price=price,
        text=question_text,
        answer=answer_text,
        question_type=question_type,
        image=image_data,
        image_filename=image_filename,
        audio=audio_data,
        audio_filename=audio_filename,
        video=video_data,
        video_filename=video_filename,
    )


def _parse_answer(question_el, ns: str) -> str:
    right_el = question_el.find('{0}right'.format(ns))
    if right_el is not None:
        answer_els = right_el.findall('{0}answer'.format(ns))
        if answer_els:
            return '/'.join((a.text or '').strip() for a in answer_els if a.text)

    answers_el = question_el.find('{0}answers'.format(ns))
    if answers_el is not None:
        right_el2 = answers_el.find('{0}right'.format(ns))
        if right_el2 is not None:
            answer_els = right_el2.findall('{0}answer'.format(ns))
            if answer_els:
                return '/'.join((a.text or '').strip() for a in answer_els if a.text)

    return ''


def _append_text(existing: str, new_part: str) -> str:
    if not existing:
        return new_part
    return existing + ' ' + new_part


def _load_media(resource_name: str, folder: str, temp_dir: str) -> Tuple[Optional[bytes], Optional[str]]:
    decoded_name = urllib.parse.unquote(resource_name)
    candidates = [
        os.path.join(temp_dir, folder, decoded_name),
        os.path.join(temp_dir, folder.lower(), decoded_name),
        os.path.join(temp_dir, decoded_name),
        os.path.join(temp_dir, folder, resource_name),
        os.path.join(temp_dir, resource_name),
    ]

    for path in candidates:
        if os.path.exists(path):
            with open(path, 'rb') as f:
                return f.read(), os.path.basename(path)

    base_name = os.path.splitext(decoded_name)[0]
    for root_dir, dirs, files in os.walk(temp_dir):
        for fname in files:
            if os.path.splitext(fname)[0].lower() == base_name.lower():
                path = os.path.join(root_dir, fname)
                with open(path, 'rb') as f:
                    return f.read(), fname

    return None, None
