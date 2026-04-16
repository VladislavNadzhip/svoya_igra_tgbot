"""
Парсер пакетов SIGame (.siq файлы).

Медиафайлы читаются напрямую из ZIP-архива (без extractall).
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
        zip_index = _build_zip_index(zf)

        content_bytes = _read_content_xml(zf)
        if content_bytes is None:
            raise ValueError("content.xml не найден в архиве")

        content = content_bytes.decode('utf-8-sig')
        root = ET.fromstring(content)

        ns = ''
        if root.tag.startswith('{'):
            ns = root.tag.split('}')[0] + '}'

        return _parse_pack(root, ns, zf, zip_index)


def _read_content_xml(zf: zipfile.ZipFile) -> Optional[bytes]:
    for name in zf.namelist():
        if name.lower() == 'content.xml' or name.lower().endswith('/content.xml'):
            return zf.read(name)
    return None


def _build_zip_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Строит индекс: нормализованное_имя -> реальное_имя_в_zip.
    Нормализация: URL-decode + lowercase.
    Хранит как полный путь, так и только basename.
    """
    index = {}

    def _add(key: str, value: str):
        if key and key not in index:
            index[key] = value

    for zip_name in zf.namelist():
        # Сырое имя
        _add(zip_name.lower(), zip_name)

        # URL-decoded полный путь
        decoded_full = urllib.parse.unquote(zip_name).lower()
        _add(decoded_full, zip_name)

        # basename (raw)
        base_raw = os.path.basename(zip_name).lower()
        _add(base_raw, zip_name)

        # basename (decoded)
        base_dec = urllib.parse.unquote(base_raw)
        _add(base_dec, zip_name)

    return index


def _normalize_resource(name: str) -> str:
    """Снимает ледящие @ и / из имени ресурса."""
    return name.lstrip('@').lstrip('/')


def _read_media_from_zip(
    zf: zipfile.ZipFile,
    zip_index: Dict[str, str],
    resource_name: str,
    folder: str
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Находит и читает медиафайл напрямую из ZIP.
    resource_name может начинаться с @ — он будет срезан.
    """
    clean = _normalize_resource(resource_name)
    decoded = urllib.parse.unquote(clean)
    base = os.path.basename(decoded)

    candidates = [
        f"{folder}/{decoded}",
        f"{folder.lower()}/{decoded}",
        f"{folder}/{clean}",
        f"{folder.lower()}/{clean}",
        decoded,
        clean,
        base,
    ]

    for candidate in candidates:
        key = candidate.lower()
        if key in zip_index:
            zip_entry = zip_index[key]
            data = zf.read(zip_entry)
            return data, os.path.basename(zip_entry)

    return None, None


def _try_all_folders(
    zf: zipfile.ZipFile,
    zip_index: Dict[str, str],
    resource_name: str
) -> Tuple[Optional[bytes], Optional[str]]:
    """Try all known media folders automatically."""
    for folder in ('Images', 'Audio', 'Video', 'Sounds'):
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
            pack_author = ', '.join(
                a.text for a in authors_el.findall('{0}author'.format(ns)) if a.text
            )
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
    round_type = 'final' if round_el.attrib.get('type', '').lower() == 'final' else 'standard'
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
        c = info_el.find('{0}comments'.format(ns))
        if c is not None and c.text:
            comment = c.text.strip()

    theme = Theme(name=theme_name, comment=comment)

    questions_el = theme_el.find('{0}questions'.format(ns))
    if questions_el is None:
        return theme

    for question_el in questions_el.findall('{0}question'.format(ns)):
        question = _parse_question(question_el, ns, zf, zip_index)
        if question:
            theme.questions.append(question)

    return theme


def _get_element_text(el) -> str:
    """
    Извлекает весь текст из элемента, включая вложенные теги (itertext).
    Это важно для атомов/шагов с вложенными тегами или CDATA.
    """
    return ''.join(el.itertext()).strip()


def _is_resource_name(text: str) -> bool:
    """
    Определяет, является ли текст именем ресурса (ссылкой на медиафайл),
    а не текстом вопроса.
    Ресурс: начинается с '@' ИЛИ содержит расширение медиафайла.
    """
    if text.startswith('@'):
        return True
    # Проверяем расширение файла
    lower = text.lower()
    media_exts = (
        '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp',  # изображения
        '.mp3', '.ogg', '.wav', '.flac', '.m4a',           # аудио
        '.mp4', '.avi', '.mkv', '.mov', '.webm',           # видео
    )
    # Считаем ресурсом только если текст похож на имя файла (нет пробелов, есть расширение)
    if ' ' not in text and any(lower.endswith(ext) for ext in media_exts):
        return True
    return False


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
            'stake': 'auction', 'secret': 'cat', 'secretnoq': 'cat',
        }
        question_type = type_map.get(qtype, 'standard')

    question_text = ''
    image_data = image_filename = None
    audio_data = audio_filename = None
    video_data = video_filename = None

    # ===== Старый формат: <scenario><atom> =====
    scenario_el = question_el.find('{0}scenario'.format(ns))
    if scenario_el is not None:
        for atom_el in scenario_el.findall('{0}atom'.format(ns)):
            atom_type = atom_el.attrib.get('type', '').lower()

            # Используем itertext() чтобы захватить текст из вложенных тегов тоже
            atom_text_raw = _get_element_text(atom_el)

            if atom_type in ('', 'text', 'say'):
                if not atom_text_raw:
                    continue
                # Проверяем: если текст выглядит как имя ресурса — пробуем загрузить как медиа
                if _is_resource_name(atom_text_raw):
                    resource = _normalize_resource(atom_text_raw)
                    data, fname = _try_all_folders(zf, zip_index, resource)
                    if data:
                        ext = fname.lower().rsplit('.', 1)[-1] if fname and '.' in fname else ''
                        if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp') and image_data is None:
                            image_data, image_filename = data, fname
                        elif ext in ('mp3', 'ogg', 'wav', 'flac', 'm4a') and audio_data is None:
                            audio_data, audio_filename = data, fname
                        elif ext in ('mp4', 'avi', 'mkv', 'mov', 'webm') and video_data is None:
                            video_data, video_filename = data, fname
                    else:
                        # Не нашли ресурс — трактуем как текст (убираем @ если есть)
                        clean = _normalize_resource(atom_text_raw)
                        question_text = _append_text(question_text, clean)
                else:
                    question_text = _append_text(question_text, atom_text_raw)

            elif atom_type == 'image':
                if atom_text_raw and image_data is None:
                    resource = _normalize_resource(atom_text_raw)
                    image_data, image_filename = _read_media_from_zip(
                        zf, zip_index, resource, 'Images'
                    )
                    if image_data is None:
                        image_data, image_filename = _try_all_folders(zf, zip_index, resource)

            elif atom_type in ('voice', 'audio'):
                if atom_text_raw and audio_data is None:
                    resource = _normalize_resource(atom_text_raw)
                    audio_data, audio_filename = _read_media_from_zip(
                        zf, zip_index, resource, 'Audio'
                    )
                    if audio_data is None:
                        audio_data, audio_filename = _try_all_folders(zf, zip_index, resource)

            elif atom_type == 'video':
                if atom_text_raw and video_data is None:
                    resource = _normalize_resource(atom_text_raw)
                    video_data, video_filename = _read_media_from_zip(
                        zf, zip_index, resource, 'Video'
                    )
                    if video_data is None:
                        video_data, video_filename = _try_all_folders(zf, zip_index, resource)

    # ===== Новый формат: <body><step> =====
    body_el = question_el.find('{0}body'.format(ns))
    if body_el is not None:
        for step_el in body_el:
            tag = step_el.tag.replace(ns, '').lower() if ns else step_el.tag.lower()
            if tag != 'step':
                continue

            # Используем itertext() — захватывает текст в т.ч. из вложенных тегов
            step_text = _get_element_text(step_el)

            if not step_text:
                continue

            if step_text.startswith('@'):
                resource_name = step_text[1:]  # срезаем @
                data, fname = _try_all_folders(zf, zip_index, resource_name)
                if data:
                    ext = fname.lower().rsplit('.', 1)[-1] if fname and '.' in fname else ''
                    if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp') and image_data is None:
                        image_data, image_filename = data, fname
                    elif ext in ('mp3', 'ogg', 'wav', 'flac', 'm4a') and audio_data is None:
                        audio_data, audio_filename = data, fname
                    elif ext in ('mp4', 'avi', 'mkv', 'mov', 'webm') and video_data is None:
                        video_data, video_filename = data, fname
                    elif image_data is None:
                        image_data, image_filename = data, fname
                else:
                    # Ресурс не найден — показываем текст как есть (без @)
                    question_text = _append_text(question_text, resource_name)
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
        answers = [a.text.strip() for a in right_el.findall('{0}answer'.format(ns)) if a.text]
        if answers:
            return '/'.join(answers)

    answers_el = question_el.find('{0}answers'.format(ns))
    if answers_el is not None:
        right_el2 = answers_el.find('{0}right'.format(ns))
        if right_el2 is not None:
            answers = [a.text.strip() for a in right_el2.findall('{0}answer'.format(ns)) if a.text]
            if answers:
                return '/'.join(answers)

    return ''


def _append_text(existing: str, new_part: str) -> str:
    return (existing + ' ' + new_part).strip() if existing else new_part
