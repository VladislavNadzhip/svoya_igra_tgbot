"""
Парсер пакетов SIGame (.siq файлы).

Медиафайлы читаются напрямую из ZIP-архива (без extractall),
что избавляет проблем с кодировкой имён файлов (кириллица, #, пробелы).
"""

import zipfile
import xml.etree.ElementTree as ET
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict


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

    with zipfile.ZipFile(file_path, 'r') as zf:
        # Строим индекс файлов архива — единажды при открытии
        zip_index = _build_zip_index(zf)

        # Читаем content.xml
        content_bytes = _read_content_xml(zf)
        if content_bytes is None:
            raise ValueError("content.xml не найден в архиве")

        content = content_bytes.decode('utf-8-sig')
        root = ET.fromstring(content)

        ns = ''
        if root.tag.startswith('{'):
            ns = root.tag.split('}')[0] + '}'

        pack = _parse_pack(root, ns, zf, zip_index)
        return pack


def _read_content_xml(zf: zipfile.ZipFile) -> Optional[bytes]:
    """Reading content.xml from zip, searching case-insensitively."""
    for name in zf.namelist():
        if name.lower() == 'content.xml' or name.lower().endswith('/content.xml'):
            return zf.read(name)
    return None


def _build_zip_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Строит словарь: lowercase_basename -> real_zipname.
    При конфликте имён победит вариант без URL-енкодинга.
    Также хранит полный путь в lowercase.
    """
    index = {}
    for zip_name in zf.namelist():
        # Полный путь lowercase
        lower_full = zip_name.lower()
        if lower_full not in index:
            index[lower_full] = zip_name

        # basename lowercase
        basename = os.path.basename(zip_name).lower()
        if basename and basename not in index:
            index[basename] = zip_name

        # URL-decoded basename lowercase
        decoded_base = urllib.parse.unquote(basename).lower()
        if decoded_base and decoded_base not in index:
            index[decoded_base] = zip_name

        # URL-decoded полный путь lowercase
        decoded_full = urllib.parse.unquote(lower_full)
        if decoded_full not in index:
            index[decoded_full] = zip_name

    return index


def _read_media_from_zip(
    zf: zipfile.ZipFile,
    zip_index: Dict[str, str],
    resource_name: str,
    folder: str
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Находит и читает медиафайл напрямую из ZIP.
    Перебирает множество вариантов путей.
    """
    decoded = urllib.parse.unquote(resource_name).lstrip('/')
    raw = resource_name.lstrip('/')
    base = os.path.basename(decoded)

    # Все ключи которые нужно проверить в индексе
    candidates = [
        # Полные пути с папкой
        f"{folder}/{decoded}",
        f"{folder.lower()}/{decoded}",
        f"{folder}/{raw}",
        f"{folder.lower()}/{raw}",
        # Только basename
        decoded,
        raw,
        base,
    ]

    for candidate in candidates:
        lower = candidate.lower()
        if lower in zip_index:
            zip_entry = zip_index[lower]
            data = zf.read(zip_entry)
            return data, os.path.basename(zip_entry)

    return None, None


def _try_all_folders(
    zf: zipfile.ZipFile,
    zip_index: Dict[str, str],
    resource_name: str
) -> Tuple[Optional[bytes], Optional[str]]:
    """Try all known media folders automatically."""
    for folder in ('Images', 'Audio', 'Video', 'Sounds', 'Video'):
        data, fname = _read_media_from_zip(zf, zip_index, resource_name, folder)
        if data:
            return data, fname
    return None, None


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


def _parse_pack(root, ns: str, zf: zipfile.ZipFile, zip_index: Dict[str, str]) -> GamePack:
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
        if comments_el is not None and comments_el.text:
            pack_comment = comments_el.text

    pack = GamePack(name=pack_name, author=pack_author, comment=pack_comment)

    rounds_el = root.find('{0}rounds'.format(ns))
    if rounds_el is None:
        return pack

    for round_el in rounds_el.findall('{0}round'.format(ns)):
        game_round = _parse_round(round_el, ns, zf, zip_index)
        if game_round and game_round.themes:
            pack.rounds.append(game_round)

    return pack


def _parse_round(round_el, ns: str, zf: zipfile.ZipFile, zip_index: Dict[str, str]) -> Round:
    round_name = round_el.attrib.get('name', 'Round')
    raw_type = round_el.attrib.get('type', 'standard').lower()

    round_type = 'final' if raw_type == 'final' else 'standard'
    game_round = Round(name=round_name, round_type=round_type)

    themes_el = round_el.find('{0}themes'.format(ns))
    if themes_el is None:
        return game_round

    for theme_el in themes_el.findall('{0}theme'.format(ns)):
        theme = _parse_theme(theme_el, ns, zf, zip_index)
        if theme and theme.questions:
            game_round.themes.append(theme)

    return game_round


def _parse_theme(theme_el, ns: str, zf: zipfile.ZipFile, zip_index: Dict[str, str]) -> Theme:
    theme_name = theme_el.attrib.get('name', 'Theme')

    comment = ''
    info_el = theme_el.find('{0}info'.format(ns))
    if info_el is not None:
        comments_el = info_el.find('{0}comments'.format(ns))
        if comments_el is not None and comments_el.text:
            comment = comments_el.text.strip()

    theme = Theme(name=theme_name, comment=comment)

    questions_el = theme_el.find('{0}questions'.format(ns))
    if questions_el is None:
        return theme

    for question_el in questions_el.findall('{0}question'.format(ns)):
        question = _parse_question(question_el, ns, zf, zip_index)
        if question:
            theme.questions.append(question)

    return theme


def _parse_question(
    question_el,
    ns: str,
    zf: zipfile.ZipFile,
    zip_index: Dict[str, str]
) -> Optional[Question]:
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
            'cat': 'cat', 'bagcat': 'bagcat', 'auction': 'auction',
            'sponsored': 'standard', 'simple': 'standard',
            'stake': 'auction', 'secret': 'cat',
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

    # ===== Старый формат: <scenario><atom> =====
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
                    image_data, image_filename = _read_media_from_zip(
                        zf, zip_index, atom_text, 'Images'
                    )

            elif atom_type in ('voice', 'audio'):
                if atom_text and audio_data is None:
                    audio_data, audio_filename = _read_media_from_zip(
                        zf, zip_index, atom_text, 'Audio'
                    )

            elif atom_type == 'video':
                if atom_text and video_data is None:
                    video_data, video_filename = _read_media_from_zip(
                        zf, zip_index, atom_text, 'Video'
                    )

    # ===== Новый формат: <body><step> =====
    body_el = question_el.find('{0}body'.format(ns))
    if body_el is not None:
        for step_el in body_el:
            tag = step_el.tag.replace(ns, '').lower() if ns else step_el.tag.lower()
            if tag != 'step':
                continue

            step_text = (step_el.text or '').strip()
            if not step_text:
                # Если текст в дочерних узлах
                inner = ' '.join(
                    (c.text or '').strip()
                    for c in step_el
                    if (c.text or '').strip()
                )
                step_text = inner.strip()

            if not step_text:
                continue

            if step_text.startswith('@'):
                resource_name = step_text[1:]
                # Пробуем все папки автоматически
                data, fname = _try_all_folders(zf, zip_index, resource_name)
                if data:
                    ext = (fname or '').lower().rsplit('.', 1)[-1] if fname else ''
                    if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp') and image_data is None:
                        image_data, image_filename = data, fname
                    elif ext in ('mp3', 'ogg', 'wav', 'flac', 'm4a') and audio_data is None:
                        audio_data, audio_filename = data, fname
                    elif ext in ('mp4', 'avi', 'mkv', 'mov', 'webm') and video_data is None:
                        video_data, video_filename = data, fname
                    elif image_data is None:
                        # Если расширение неизвестно — пробуем как картинку
                        image_data, image_filename = data, fname
                else:
                    # Файл не найден — оставляем как текст (название ресурса)
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
