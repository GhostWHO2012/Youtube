"""YouTube Downloader Web Application"""

import csv
import json
import base64
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
import winreg
import zipfile
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    Response,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024
DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)
PROJECTS_DIR = DOWNLOADS_DIR / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

PROJECT_SUBDIRS = {
    "source": "00_源文件",
    "video": "01_视频",
    "audio": "02_音频",
    "subtitles": "03_字幕",
    "description": "04_简介",
    "translation": "10_翻译",
    "burned": "30_烧录",
    "clips": "40_切片",
}

LEGACY_PROJECT_SUBDIRS = {
    "source": "00_source",
    "video": "01_video",
    "audio": "02_audio",
    "subtitles": "03_subtitles",
    "description": "04_description",
    "translation": "10_translation",
    "burned": "30_burned",
    "clips": "40_clips",
}

tasks = {}
task_lock = threading.Lock()
local_subtitle_tasks = {}
local_subtitle_sessions = {}
local_subtitle_lock = threading.Lock()
font_file_cache = {}
font_measure_cache = {}


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def normalize_project_name_text(name):
    text = str(name or "")
    replacements = {
        "\uff1a": ":",
        "\ufe55": ":",
        "\u02f8": ":",
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u300c": '"',
        "\u300d": '"',
        "\u300e": '"',
        "\u300f": '"',
        "\uff02": '"',
        "\uff07": "'",
        "\uff5c": "|",
        "\u2215": "/",
        "\u2044": "/",
        "\uff0f": "/",
        "\uff3c": "\\",
        "\uff1f": "?",
        "\uff0a": "*",
        "\uff1c": "<",
        "\uff1e": ">",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def project_safe_name(name):
    safe = sanitize_filename(normalize_project_name_text(name)).strip().strip(".")
    safe = re.sub(r"\s+", " ", safe)
    return safe[:120] or f"project_{time.strftime('%Y%m%d_%H%M%S')}"


def project_match_key(name):
    safe = project_safe_name(name).casefold()
    safe = re.sub(r"[_\-\s.]+", "", safe)
    return safe


def normalize_local_project_title(title="", original_name="", fallback="local_video"):
    base = (title or "").strip()
    if not base and original_name:
        base = Path(original_name).stem
    base = base or fallback
    base = re.sub(r"\s+", " ", base).strip().strip(".")
    for _ in range(4):
        cleaned = re.sub(r"(?i)\.(audio|source|video)$", "", base).strip().strip(".")
        if cleaned == base:
            break
        base = cleaned
    return base or fallback


def local_media_output_stem(name="", fallback="subtitle"):
    raw = Path(str(name or "")).stem if name else ""
    raw = re.sub(r"(?i)\.local\.(bilingual(?:\.(en-top|zh-top))?|zh|en)$", "", raw)
    raw = re.sub(r"(?i)\.(raw_en|split_en|merged_en|corrected_en|compare_zh|zh)$", "", raw)
    for _ in range(4):
        cleaned = re.sub(r"\.(英文原版|有翻译参考资料版|无翻译参考资料版|中文字幕|双语|中文在上|英文在上|对比翻译)$", "", raw)
        if cleaned == raw:
            break
        raw = cleaned
    stem = normalize_local_project_title(raw, "", fallback)
    return sanitize_filename(stem) or fallback


def ensure_project_dirs(title):
    safe_name = project_safe_name(title)
    project_dir = PROJECTS_DIR / safe_name
    if not project_dir.exists():
        target_key = project_match_key(safe_name)
        for existing in PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else []:
            if existing.is_dir() and project_match_key(existing.name) == target_key:
                project_dir = existing
                break
    project_dir.mkdir(parents=True, exist_ok=True)
    subdirs = {}
    for key, folder in PROJECT_SUBDIRS.items():
        path = project_dir / folder
        path.mkdir(parents=True, exist_ok=True)
        subdirs[key] = path
    return project_dir, subdirs


def project_subdir_candidates(project_dir, key, create_primary=False):
    project_dir = Path(project_dir)
    names = [PROJECT_SUBDIRS.get(key), LEGACY_PROJECT_SUBDIRS.get(key)]
    paths = []
    for name in names:
        if not name:
            continue
        path = project_dir / name
        if path not in paths:
            paths.append(path)
    if create_primary and paths:
        paths[0].mkdir(parents=True, exist_ok=True)
    return paths


def project_subdir(project_dir, key):
    return project_subdir_candidates(project_dir, key, create_primary=True)[0]


def rel_download_path(path):
    try:
        return str(Path(path).resolve().relative_to(DOWNLOADS_DIR.resolve())).replace("\\", "/")
    except Exception:
        return Path(path).name


def make_download_zip(path):
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    zip_path = path.with_suffix(path.suffix + ".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(path, arcname=path.name)
    return zip_path


def file_download_payload(path, prefer_zip=False):
    path = Path(path)
    file_rel = rel_download_path(path)
    payload = {
        "file": file_rel,
        "url": f"/files/{file_rel}",
    }
    if prefer_zip:
        zip_path = make_download_zip(path)
        if zip_path:
            zip_rel = rel_download_path(zip_path)
            payload["zip_file"] = zip_rel
            payload["zip_url"] = f"/files/{zip_rel}"
            payload["url"] = payload["zip_url"]
    return payload


def path_download_info(path):
    path = Path(path)
    try:
        rel = str(path.resolve().relative_to(DOWNLOADS_DIR.resolve())).replace("\\", "/")
        return {
            "file": rel,
            "url": f"/files/{urllib.parse.quote(rel, safe='/')}",
            "path": str(path),
        }
    except Exception:
        return {"file": str(path), "url": "", "path": str(path)}


def resolve_download_directory_candidate(path):
    if not path:
        return None
    candidate = Path(str(path)).expanduser()
    if not candidate.is_absolute():
        candidate = DOWNLOADS_DIR / candidate
    try:
        candidate = candidate.resolve()
    except Exception:
        return None
    if candidate.exists() and candidate.is_file():
        candidate = candidate.parent.resolve()
    try:
        candidate.relative_to(DOWNLOADS_DIR.resolve())
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_dir():
        return None
    return candidate


def open_directory_with_system_manager(path):
    if os.name == "nt":
        os.startfile(str(path))
        return
    opener = shutil.which("xdg-open") or shutil.which("open")
    if not opener:
        raise RuntimeError("No system file manager opener is available")
    subprocess.Popen([opener, str(path)])


def seconds_to_desc_time(seconds):
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def extract_timeline_from_info(info, fallback_timeline=""):
    chapters = info.get("chapters") or []
    lines = []
    for chapter in chapters:
        start = seconds_to_desc_time(chapter.get("start_time", 0))
        title = (chapter.get("title") or "").strip()
        if title:
            lines.append(f"{start} {title}")

    if lines:
        return "\n".join(lines)

    desc = info.get("description") or ""
    timeline_lines = []
    pattern = re.compile(r"(?:(?:\d{1,2}:)?\d{1,2}:\d{2})")
    for raw in desc.splitlines():
        line = raw.strip()
        if pattern.search(line):
            timeline_lines.append(line)

    if timeline_lines:
        return "\n".join(timeline_lines)
    return fallback_timeline or ""


def build_bilibili_description(info, url, timeline=""):
    title = info.get("title", "")
    uploader = info.get("uploader") or info.get("channel") or ""
    desc = (info.get("description") or "").strip()
    parts = []
    if title:
        parts.append(f"原视频标题：{title}")
    if uploader:
        parts.append(f"原作者/频道：{uploader}")
    if url:
        parts.append(f"原视频链接：{url}")
    if desc:
        parts.append("\n原视频简介：\n" + desc)
    if timeline:
        parts.append("\n时间轴：\n" + timeline)
    parts.append("\n说明：本文件由本地项目自动整理，可继续用于字幕翻译、双语字幕和切片。")
    return "\n".join(parts).strip() + "\n"


def format_duration_zh(duration):
    try:
        total = max(0, int(float(duration or 0)))
    except Exception:
        total = 0
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def generate_ai_description_text(
    info,
    url,
    api_url,
    api_key,
    model,
    style="",
    include_timeline=False,
    timeline="",
    bilibili=True,
):
    if not api_key:
        raise ValueError("请先填写翻译/简介 API Key")
    title = info.get("title", "")
    desc = info.get("description", "")
    tags = info.get("tags", []) or []
    duration_str = format_duration_zh(info.get("duration", 0))
    prompt = (
        "你是一个专业的视频简介撰写和英译中编辑。\n"
        "请把目标视频的英文信息整理成中文简介，并先给出结合背景的标题翻译。\n"
        "保留 AI、技术、产品、人名、公司名、模型名等关键词的准确性；专属名称默认保留英文，不要强行音译。\n\n"
    )
    if style:
        prompt += f"## 参考风格\n{style}\n\n"
    prompt += (
        f"## 目标视频信息\n"
        f"- 原视频标题: {title}\n"
        f"- 原视频链接: {url}\n"
        f"- 时长: {duration_str}\n"
        f"- 标签: {', '.join(tags[:12]) if tags else '无'}\n\n"
        f"## 原始简介\n{desc or '无'}\n\n"
    )
    if include_timeline:
        if timeline:
            prompt += (
                f"## 可用时间轴\n{timeline}\n\n"
                "请把时间轴概括成适合 B站简介的中文时间轴，每行使用“00:00 中文标题”的形式。\n\n"
            )
        else:
            prompt += "该视频暂时没有可用时间轴，如简介需要时间轴，请只给出合理的章节建议，不要编造具体不存在的时间点。\n\n"
    if bilibili:
        prompt += (
            "请输出可以直接粘贴到 B站简介区的纯文本，格式要求：\n"
            "1. 开头必须先输出“标题翻译：”小节，包含四行：\n"
            "   - 英文原题：保留原视频英文标题。\n"
            "   - 中文标题：结合视频背景翻译出的中文标题，适合 B站读者，但不要标题党。\n"
            "   - 中英文对照：用简短文字说明英文标题核心词和中文标题如何对应。\n"
            "   - 翻译理由：说明为什么这样翻译，必须结合原始简介、标签、人物/公司/产品背景和视频主题。\n"
            "2. 然后给出 1-2 句中文内容简介，不要写营销口号。\n"
            "3. 用“看点：”列出 3-6 个简短要点。\n"
            "4. 如果有时间轴，用“时间轴：”列出。\n"
            "5. 末尾保留“原视频标题：”“原视频链接：”。\n"
            "6. 不要使用 Markdown 标题符号，不要输出解释过程。\n"
        )
    else:
        prompt += (
            "请输出自然、准确的中文简介，并在开头包含：英文原题、中文标题、中英文对照、翻译理由。"
            "标题翻译理由必须结合原始简介、标签、人物/公司/产品背景和视频主题。不要输出额外解释过程。"
        )

    from openai import OpenAI
    client_kwargs = {"api_key": api_key}
    if api_url:
        client_kwargs["base_url"] = api_url
    client = OpenAI(**client_kwargs)
    request_kwargs = {
        "model": model or "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": 2000,
    }
    try:
        response = client.chat.completions.create(**request_kwargs)
    except Exception as exc:
        if "max_completion_tokens" not in str(exc):
            raise
        request_kwargs["max_tokens"] = request_kwargs.pop("max_completion_tokens")
        response = client.chat.completions.create(**request_kwargs)

    generated = (response.choices[0].message.content or "").strip()
    if bilibili:
        source_lines = []
        if title and "原视频标题" not in generated:
            source_lines.append(f"原视频标题：{title}")
        if url and "原视频链接" not in generated:
            source_lines.append(f"原视频链接：{url}")
        if source_lines:
            generated = f"{generated}\n\n" + "\n".join(source_lines)
    return generated.strip() + "\n"


def save_ai_description_file(project_dir, subdirs, info, generated, bilibili=True):
    filename = "translated_bilibili_description.txt" if bilibili else "translated_description.txt"
    output_path = subdirs["description"] / filename
    title = info.get("title") or project_dir.name
    output_path.write_text(
        f"{generated.strip()}\n\n保存时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n项目标题：{title}\n",
        encoding="utf-8",
    )
    return output_path


def generate_and_save_ai_description(project_dir, subdirs, info, url, options, timeline=""):
    generated = generate_ai_description_text(
        info,
        url,
        options.get("api_url", ""),
        options.get("api_key", ""),
        options.get("model", "gpt-4o-mini"),
        style=options.get("style", ""),
        include_timeline=bool(options.get("include_timeline", True)),
        timeline=timeline,
        bilibili=bool(options.get("bilibili", True)),
    )
    return save_ai_description_file(
        project_dir,
        subdirs,
        info,
        generated,
        bilibili=bool(options.get("bilibili", True)),
    ), generated


def write_project_notes(
    project_dir,
    subdirs,
    info=None,
    url="",
    timeline="",
    save_description=True,
    save_link_title=True,
):
    info = info or {}
    title = info.get("title") or project_dir.name
    meta = {
        "title": title,
        "url": url,
        "uploader": info.get("uploader", ""),
        "channel": info.get("channel", ""),
        "duration": info.get("duration", 0),
        "upload_date": info.get("upload_date", ""),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project_dir": str(project_dir),
    }
    (subdirs["source"] / "source_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if save_link_title:
        (subdirs["source"] / "link_title.txt").write_text(
            f"标题：{title}\n链接：{url or ''}\n",
            encoding="utf-8",
        )
    if save_description:
        (subdirs["description"] / "bilibili_description.txt").write_text(
            build_bilibili_description(info, url, timeline),
            encoding="utf-8",
        )
        (subdirs["description"] / "timeline_outline.md").write_text(
            f"# {title}\n\n## 时间轴大纲\n\n{timeline or '暂无时间轴，后续可由字幕分析生成。'}\n",
            encoding="utf-8",
        )


def fetch_video_info(url):
    result = subprocess.run(
        ["yt-dlp", "-j", "--no-warnings", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}


def target_dir_for_project_file(project_dir, subdirs, path):
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        return subdirs["translation"] if ".hardsub" in name else subdirs["video"]
    if suffix in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".opus", ".ogg"):
        return subdirs["audio"]
    if suffix in (".srt", ".vtt"):
        return subdirs["subtitles"]
    if suffix in (".ass",):
        return subdirs["translation"] if ".dual" in name else subdirs["subtitles"]
    if suffix in (".description", ".txt", ".md"):
        return subdirs["description"]
    if suffix in (".json", ".jpg", ".jpeg", ".png", ".webp"):
        return subdirs["source"]
    if name.endswith(".log"):
        return subdirs["source"]
    return subdirs["source"]


def move_project_outputs(project_dir, subdirs):
    for path in list(project_dir.iterdir()):
        if path.is_dir():
            continue
        target_dir = target_dir_for_project_file(project_dir, subdirs, path)
        target = target_dir / path.name
        if target.exists():
            target = target_dir / f"{path.stem}.{int(time.time())}{path.suffix}"
        try:
            path.replace(target)
        except Exception:
            shutil.copy2(path, target)
            try:
                path.unlink()
            except Exception:
                pass


def normalize_cover_images_to_jpeg(source_dir, update=None):
    """Convert downloaded thumbnails to .jpeg so cover output is predictable."""
    converted = []
    image_suffixes = {".webp", ".png", ".jpg", ".jpeg"}
    for image in sorted(Path(source_dir).glob("*")):
        if not image.is_file() or image.suffix.lower() not in image_suffixes:
            continue
        if image.suffix.lower() == ".jpeg":
            converted.append(image)
            continue

        target = image.with_suffix(".jpeg")
        if target.exists():
            target = image.with_name(f"{image.stem}.{int(time.time())}.jpeg")
        if update:
            update("downloading", 98, f"Converting cover to JPEG: {image.name}")
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(image),
                "-frames:v", "1", "-q:v", "2",
                str(target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
            image.unlink(missing_ok=True)
            converted.append(target)
        else:
            target.unlink(missing_ok=True)
    return converted


def find_primary_video(search_dir):
    candidates = [
        p for p in search_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".m4v")
        and ".hardsub" not in p.name.lower()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def normalize_audio_format(audio_format):
    return "mp3"


def audio_export_args(audio_format):
    return "mp3", ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]


def standardize_mp3_file(path, update=None):
    """Rewrite MP3 as a Windows-friendly MP3 file."""
    path = Path(path)
    if path.suffix.lower() != ".mp3" or not path.exists() or path.stat().st_size <= 0:
        return False
    tmp = path.with_name(f"{path.stem}.standardizing{path.suffix}")
    if update:
        update("downloading", 97, f"Standardizing MP3: {path.name}")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(path),
            "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-id3v2_version", "3", "-write_id3v1", "1",
            str(tmp),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(path)
        return True
    tmp.unlink(missing_ok=True)
    return False


def standardize_project_mp3_files(project_dir, update=None):
    changed = 0
    for mp3 in sorted(Path(project_dir).rglob("*.mp3")):
        if ".standardizing" in mp3.name:
            continue
        if standardize_mp3_file(mp3, update):
            changed += 1
    return changed


def ensure_audio_from_video(project_dir, subdirs, audio_format="mp3", update=None):
    video = find_primary_video(project_dir)
    if not video:
        return None
    audio_ext, ffmpeg_audio_args = audio_export_args(audio_format)
    audio_path = subdirs["audio"] / f"{sanitize_filename(video.stem)}.audio.{audio_ext}"
    if audio_path.exists() and audio_path.stat().st_size > 0:
        return audio_path
    if update:
        update("downloading", 96, f"Extracting {audio_ext.upper()} audio...")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video),
            *ffmpeg_audio_args,
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if result.returncode != 0 or not audio_path.exists():
        return None
    return audio_path


def translation_reference_used(context):
    context = context or {}
    return bool(
        str(context.get("translation_reference") or "").strip()
        or str(context.get("reference_materials") or "").strip()
    )


def translation_version_label(context):
    return "有翻译参考资料版" if translation_reference_used(context) else "无翻译参考资料版"


def translation_model_label(context):
    context = context or {}
    model = (
        str(context.get("translation_model") or "").strip()
        or str(context.get("model") or "").strip()
    )
    if not model:
        return ""
    model = model.split("/")[-1].strip()
    model = re.sub(r"(?i)^claude-", "", model)
    model = re.sub(r"(?i)^anthropic-", "", model)
    model = re.sub(r"(?i)^openai-", "", model)
    model = re.sub(r"(?i)^gpt-", "GPT", model)
    model = re.sub(r"(?i)^opus-", "OPUS", model)
    model = re.sub(r"(?i)^sonnet-", "SONNET", model)
    model = re.sub(r"(?i)^haiku-", "HAIKU", model)
    model = re.sub(r"(?i)opus-(\d+(?:\.\d+)*)", r"OPUS\1", model)
    model = re.sub(r"(?i)sonnet-(\d+(?:\.\d+)*)", r"SONNET\1", model)
    model = re.sub(r"(?i)haiku-(\d+(?:\.\d+)*)", r"HAIKU\1", model)
    model = re.sub(r"(?i)GPT(\d)", r"GPT\1", model)
    model = re.sub(r"[-_\s]+", "-", model).strip("-_. ")
    model = sanitize_filename(model).replace("_", "-")
    return model[:48]


def subtitle_output_filename(stem, output_mode, order="zh_top", context=None, compare=False):
    stem = local_media_output_stem(stem, "subtitle")
    if output_mode == "en":
        return f"{stem}.英文原版.srt"

    version = translation_version_label(context)
    model_label = translation_model_label(context)
    model_prefix = f"{model_label}" if model_label else ""
    if compare:
        return f"{stem}.{model_prefix}{version}.对比翻译.中文字幕.srt"
    if output_mode == "zh":
        return f"{stem}.{model_prefix}{version}.中文字幕.srt"

    order_label = "中文在上" if order == "zh_top" else "英文在上"
    return f"{stem}.{model_prefix}{version}.双语.{order_label}.srt"


def save_session_translation_artifacts(session, stem="subtitle"):
    project_dir = session.get("project_dir")
    translation_dir = session.get("translation_dir")
    if not translation_dir:
        return {}
    translation_dir = Path(translation_dir)
    translation_dir.mkdir(parents=True, exist_ok=True)
    stem = local_media_output_stem(stem or session.get("name"), "subtitle")
    entries = session.get("entries", [])
    outputs = {}
    source_entries = [{**e, "translation": ""} for e in entries]
    raw_en = entries_to_srt(source_entries, bilingual=False, order="en_top")
    if raw_en.strip():
        path = translation_dir / subtitle_output_filename(stem, "en", context=session)
        path.write_text(raw_en, encoding="utf-8")
        outputs["english_original"] = path
    if any((e.get("translation") or "").strip() for e in entries):
        zh = entries_to_srt([{**e, "source": e.get("translation", ""), "translation": ""} for e in entries], bilingual=False)
        zh_path = translation_dir / subtitle_output_filename(stem, "zh", context=session)
        zh_path.write_text(zh, encoding="utf-8")
        outputs["zh"] = zh_path
        bi_path = translation_dir / subtitle_output_filename(stem, "bilingual", order="zh_top", context=session)
        bi_path.write_text(entries_to_srt(entries, bilingual=True, order="zh_top"), encoding="utf-8")
        outputs["bilingual_zh_top"] = bi_path
    if project_dir:
        (translation_dir / "translation_manifest.json").write_text(
            json.dumps(
                {
                    "project_dir": project_dir,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "translation_model": session.get("translation_model") or session.get("model", ""),
                    "files": {k: rel_download_path(v) for k, v in outputs.items()},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return outputs


def get_subtitle_timeline(url):
    """Fetch auto-generated subtitles and return a timeline text."""
    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return ""

        info = json.loads(result.stdout)
        auto_caps = info.get("automatic_captions", {})
        if not auto_caps:
            return ""

        lang = next((l for l in ("zh", "zh-Hans", "en") if l in auto_caps), next(iter(auto_caps), None))
        if not lang:
            return ""

        formats = auto_caps[lang]
        json3_url = next((f["url"] for f in formats if f.get("ext") == "json3"), None)
        if not json3_url:
            return ""

        resp = urllib.request.urlopen(json3_url, timeout=15)
        sub_data = json.loads(resp.read().decode("utf-8"))

        chunks = []
        current_start = None
        current_text = []

        for event in sub_data.get("events", []):
            start_ms = event.get("tStartMs", 0)
            segs = event.get("segs", [])
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if not text or text == "\n":
                continue

            if current_start is None:
                current_start = start_ms

            if start_ms - current_start >= 30000:
                minutes = current_start // 60000
                seconds = (current_start % 60000) // 1000
                chunks.append(f"{minutes:02d}:{seconds:02d} {' '.join(current_text)}")
                current_start = start_ms
                current_text = [text]
            else:
                current_text.append(text)

        if current_text and current_start is not None:
            minutes = current_start // 60000
            seconds = (current_start % 60000) // 1000
            chunks.append(f"{minutes:02d}:{seconds:02d} {' '.join(current_text)}")

        return "\n".join(chunks)
    except Exception:
        return ""


# ── Subtitle Processing ──────────────────────────────────────────────

def parse_time(time_str):
    """Convert SRT/VTT timestamp to seconds."""
    time_str = time_str.strip().replace(',', '.')
    parts = time_str.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return 0


def parse_clip_time(value):
    """Parse clip time as seconds, MM:SS, HH:MM:SS, with optional milliseconds."""
    text = str(value or "").strip().replace(",", ".")
    if not text:
        raise ValueError("时间不能为空")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError(f"时间格式不正确：{value}")


def clip_time_label(seconds):
    return format_srt_time(seconds).replace(",", ".")


def format_ass_time(seconds):
    """Format seconds to ASS timestamp H:MM:SS.CC."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def format_srt_time(seconds):
    """Format seconds to SRT timestamp HH:MM:SS,mmm."""
    seconds = max(0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        s += 1
        ms -= 1000
    if s >= 60:
        m += 1
        s -= 60
    if m >= 60:
        h += 1
        m -= 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def probe_video_dimensions(video_path):
    """Return (width, height) for a local video, or the ASS default."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            width, height = result.stdout.strip().splitlines()[0].split(",")[:2]
            width, height = int(width), int(height)
            if width > 0 and height > 0:
                return width, height
    except Exception:
        pass
    return 1920, 1080


def parse_subtitle_file(filepath):
    """Parse SRT or VTT into [(start, end, text), ...]."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    if content.startswith('WEBVTT'):
        idx = content.find('\n\n')
        content = content[idx + 2:] if idx >= 0 else ''

    pattern = (
        r'(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*'
        r'(\d{1,2}:\d{2}:\d{2}[.,]\d{3})[^\n]*\n'
        r'((?:(?!\n\n|\n\d+\s*\n).)+)'
    )
    segments = []
    for match in re.finditer(pattern, content, re.DOTALL):
        start = parse_time(match.group(1))
        end = parse_time(match.group(2))
        text = match.group(3).strip()
        text = re.sub(r'<[^>]+>', '', text)
        lines = []
        for line in text.split('\n'):
            line = line.strip()
            if line and line not in lines:
                lines.append(line)
        text = ' '.join(lines)
        if text:
            segments.append((start, end, text))

    return segments


def merge_subtitle_tracks(zh_segs, en_segs):
    """Time-align two subtitle tracks into [(start, end, zh, en), ...]."""
    if not zh_segs and not en_segs:
        return []

    times = set()
    for s, e, _ in zh_segs:
        times.add(round(s, 3)); times.add(round(e, 3))
    for s, e, _ in en_segs:
        times.add(round(s, 3)); times.add(round(e, 3))
    times = sorted(times)
    if len(times) < 2:
        return []

    merged = []
    for i in range(len(times) - 1):
        start, end = times[i], times[i + 1]
        if end - start < 0.01:
            continue

        zh_text = ''
        for s, e, t in zh_segs:
            if s <= start + 0.05 and e >= end - 0.05:
                zh_text = t.replace('\n', ' ')
                break

        en_text = ''
        for s, e, t in en_segs:
            if s <= start + 0.05 and e >= end - 0.05:
                en_text = t.replace('\n', ' ')
                break

        if zh_text or en_text:
            merged.append((start, end, zh_text, en_text))

    # merge consecutive segments with identical content
    result = []
    for seg in merged:
        if result and result[-1][2] == seg[2] and result[-1][3] == seg[3]:
            result[-1] = (result[-1][0], seg[1], seg[2], seg[3])
        else:
            result.append(list(seg))
    return result


def hex_to_ass(hex_color):
    """Convert #RRGGBB to ASS &H00BBGGRR."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


def hex_to_ass_bgr(hex_color):
    """Convert #RRGGBB to ASS inline BGR (BBGGRR)."""
    h = (hex_color or "#000000").lstrip('#')
    if len(h) != 6:
        h = "000000"
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{b:02X}{g:02X}{r:02X}"


def clamp_int(value, min_value, max_value, default):
    try:
        value = int(value)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def normalize_font_label(name):
    name = str(name or "").replace(" (TrueType)", "").replace(" (OpenType)", "")
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


def windows_font_dirs():
    dirs = []
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    dirs.append(Path(windir) / "Fonts")
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        dirs.append(Path(local_app) / "Microsoft" / "Windows" / "Fonts")
    dirs.append(Path(os.path.expanduser("~/Fonts")))
    return dirs


def resolve_font_file(font_name):
    key = normalize_font_label(font_name)
    if not key:
        return None
    if key in font_file_cache:
        return font_file_cache[key]

    matches = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            reg_key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except Exception:
            continue
        i = 0
        while True:
            try:
                name, value, _type = winreg.EnumValue(reg_key, i)
            except OSError:
                break
            clean_name = normalize_font_label(name)
            if key == clean_name or key in clean_name or clean_name in key:
                matches.append(str(value))
            i += 1
        try:
            winreg.CloseKey(reg_key)
        except Exception:
            pass

    font_dirs = windows_font_dirs()
    for value in matches:
        candidate = Path(value)
        candidates = [candidate] if candidate.is_absolute() else [d / value for d in font_dirs]
        for item in candidates:
            if item.exists():
                font_file_cache[key] = str(item)
                return font_file_cache[key]

    compact = re.sub(r"[^a-z0-9]+", "", key)
    for directory in font_dirs:
        if not directory.exists():
            continue
        for item in directory.glob("*"):
            if item.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            stem = re.sub(r"[^a-z0-9]+", "", item.stem.lower())
            if compact and (compact == stem or compact in stem or stem in compact):
                font_file_cache[key] = str(item)
                return font_file_cache[key]

    font_file_cache[key] = None
    return None


def measure_text_width_with_font(text, font_size, font_name):
    cache_key = (normalize_font_label(font_name), int(font_size))
    try:
        from PIL import ImageFont
    except Exception:
        return None

    font = font_measure_cache.get(cache_key)
    if font is None:
        font_path = resolve_font_file(font_name) or font_name
        for candidate in (font_path, "Microsoft YaHei", "msyh.ttc", "Arial"):
            try:
                font = ImageFont.truetype(candidate, int(font_size))
                break
            except Exception:
                font = None
        if font is None:
            font_measure_cache[cache_key] = False
            return None
        font_measure_cache[cache_key] = font
    if font is False:
        return None

    try:
        if hasattr(font, "getlength"):
            return float(font.getlength(str(text or "")))
        bbox = font.getbbox(str(text or ""))
        return float(bbox[2] - bbox[0])
    except Exception:
        return None


def ass_escape_text(text):
    """Escape text that would otherwise be parsed as ASS override tags."""
    text = str(text or "").replace("\r", " ").replace("\n", " ")
    text = text.replace("{", "（").replace("}", "）")
    return text.strip()


def estimate_ass_text_width(text, font_size, letter_spacing=0, font_name=None):
    """Estimate rendered text width for background placement."""
    chars = list(str(text or ""))
    if font_name:
        measured = measure_text_width_with_font(text, font_size, font_name)
        if measured is not None:
            if len(chars) > 1:
                measured += (len(chars) - 1) * float(letter_spacing or 0)
            return max(measured * 1.02, font_size)

    width = 0.0
    for ch in chars:
        code = ord(ch)
        if ch.isspace():
            width += font_size * 0.32
        elif code >= 0x4E00 and code <= 0x9FFF:
            width += font_size
        elif code > 127:
            width += font_size * 0.82
        elif ch in "ilI.,'!:;|`":
            width += font_size * 0.28
        elif ch in "mwMW@#%&QG":
            width += font_size * 0.86
        elif ch.isupper():
            width += font_size * 0.62
        else:
            width += font_size * 0.52
    if len(chars) > 1:
        width += (len(chars) - 1) * float(letter_spacing or 0)
    return max(width, font_size)


def wrap_subtitle_text(text, font_size, max_width, letter_spacing=0, font_name=None):
    """Wrap one subtitle line so ASS and the custom background agree."""
    text = ass_escape_text(text)
    if not text:
        return []
    if estimate_ass_text_width(text, font_size, letter_spacing, font_name) <= max_width:
        return [text]

    chunks = []
    current = ""
    tokens = re.findall(r"\S+\s*", text)
    for token in tokens:
        candidate = current + token
        if current and estimate_ass_text_width(candidate.strip(), font_size, letter_spacing, font_name) > max_width:
            chunks.append(current.strip())
            current = token
        else:
            current = candidate

        while estimate_ass_text_width(current.strip(), font_size, letter_spacing, font_name) > max_width and len(current.strip()) > 1:
            part = ""
            for ch in current.strip():
                if part and estimate_ass_text_width(part + ch, font_size, letter_spacing, font_name) > max_width:
                    break
                part += ch
            chunks.append(part.strip())
            current = current.strip()[len(part):].lstrip()

    if current.strip():
        chunks.append(current.strip())
    return chunks or [text]


def rounded_rect_ass_path(width, height, radius):
    """Build an ASS vector path for a rounded rectangle centered on origin."""
    width = max(1, round(width))
    height = max(1, round(height))
    radius = max(0, min(round(radius), width // 2, height // 2))
    x1, y1 = -width // 2, -height // 2
    x2, y2 = x1 + width, y1 + height
    if radius <= 0:
        return f"m {x1} {y1} l {x2} {y1} l {x2} {y2} l {x1} {y2}"

    k = 0.55228475
    c = round(radius * k)
    r = radius
    return (
        f"m {x1 + r} {y1} "
        f"l {x2 - r} {y1} "
        f"b {x2 - r + c} {y1} {x2} {y1 + r - c} {x2} {y1 + r} "
        f"l {x2} {y2 - r} "
        f"b {x2} {y2 - r + c} {x2 - r + c} {y2} {x2 - r} {y2} "
        f"l {x1 + r} {y2} "
        f"b {x1 + r - c} {y2} {x1} {y2 - r + c} {x1} {y2 - r} "
        f"l {x1} {y1 + r} "
        f"b {x1} {y1 + r - c} {x1 + r - c} {y1} {x1 + r} {y1}"
    )


def rounded_rect_ass_path_origin(width, height, radius):
    """Build an ASS vector path for a rounded rectangle from top-left origin."""
    width = max(1, round(width))
    height = max(1, round(height))
    radius = max(0, min(round(radius), width // 2, height // 2))
    x1, y1 = 0, 0
    x2, y2 = width, height
    if radius <= 0:
        return f"m {x1} {y1} l {x2} {y1} l {x2} {y2} l {x1} {y2}"

    k = 0.55228475
    c = round(radius * k)
    r = radius
    return (
        f"m {x1 + r} {y1} "
        f"l {x2 - r} {y1} "
        f"b {x2 - r + c} {y1} {x2} {y1 + r - c} {x2} {y1 + r} "
        f"l {x2} {y2 - r} "
        f"b {x2} {y2 - r + c} {x2 - r + c} {y2} {x2 - r} {y2} "
        f"l {x1 + r} {y2} "
        f"b {x1 + r - c} {y2} {x1} {y2 - r + c} {x1} {y2 - r} "
        f"l {x1} {y1 + r} "
        f"b {x1} {y1 + r - c} {x1 + r - c} {y1} {x1 + r} {y1}"
    )


def subtitle_anchor_center(alignment, play_res_x, play_res_y, side_margin, margin_v, block_w, block_h):
    """Return the center of the text block for an ASS alignment value."""
    alignment = int(alignment or 2)
    col = alignment % 3
    if col == 1:
        x = side_margin + block_w / 2
    elif col == 2:
        x = play_res_x / 2
    else:
        x = play_res_x - side_margin - block_w / 2

    if alignment in (7, 8, 9):
        y = margin_v + block_h / 2
    elif alignment in (4, 5, 6):
        y = play_res_y / 2
    else:
        y = play_res_y - margin_v - block_h / 2
    return x, y


def generate_dual_ass(merged_segs, opts):
    """Generate ASS content with configurable language order."""
    font = opts.get('font', 'Microsoft YaHei') or 'Microsoft YaHei'
    needs_cjk_font = any(
        any("\u4e00" <= ch <= "\u9fff" for ch in str(seg[2] or ""))
        for seg in merged_segs
    )
    if needs_cjk_font and font.strip().lower() in {
        "arial", "calibri", "verdana", "tahoma", "times new roman", "segoe ui"
    }:
        font = "Microsoft YaHei"
    size = clamp_int(opts.get('size', 52), 16, 200, 52)
    color = hex_to_ass(opts.get('color', '#FFFFFF'))
    outline = hex_to_ass(opts.get('outline_color', '#000000'))
    sub_order = opts.get('sub_order', 'zh_top')
    sub_pos = clamp_int(opts.get('sub_pos', 2), 1, 9, 2)
    margin_v = clamp_int(opts.get('margin_v', 30), 0, 1000, 30)
    letter_spacing = clamp_int(opts.get('letter_spacing', 0), -20, 100, 0)
    line_spacing = clamp_int(opts.get('line_spacing', 0), -80, 200, 0)
    play_res_x = int(opts.get('play_res_x', 1920) or 1920)
    play_res_y = int(opts.get('play_res_y', 1080) or 1080)
    side_margin = max(10, round(play_res_x * 30 / 1920))

    bg_enabled = opts.get('bg_enabled', False)
    bg_color_hex = opts.get('bg_color', '#000000')
    bg_opacity = clamp_int(opts.get('bg_opacity', 50), 0, 100, 50)
    bg_radius = clamp_int(opts.get('bg_radius', 30), 0, 100, 30)
    bg_width_pct = clamp_int(opts.get('bg_width', 80), 0, 100, 80)
    bg_height_pct = clamp_int(opts.get('bg_height', 20), 0, 100, 20)
    bg_offset_x = clamp_int(opts.get('bg_offset_x', 0), -100, 100, 0)
    bg_offset_y = clamp_int(opts.get('bg_offset_y', 0), -100, 100, 0)
    bg_alpha = int(round((1 - bg_opacity / 100) * 255))
    outline_val = max(2, round(size * 0.045, 1))
    max_text_width = max(size * 8, play_res_x - side_margin * 2 - size)

    ass = (
        "[Script Info]\n"
        "Title: Dual Subtitles (ZH+EN)\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{color},{color},"
        f"{outline},&H80000000,-1,0,0,0,100,100,{letter_spacing},0,1,{outline_val},1,"
        f"{sub_pos},{side_margin},{side_margin},{margin_v},1\n\n"
        f"Style: Bg,Arial,10,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    for seg in merged_segs:
        start_str = format_ass_time(seg[0])
        end_str = format_ass_time(seg[1])
        zh, en = seg[2], seg[3]

        parts = []
        if sub_order == 'en_top':
            if en:
                parts.append(en)
            if zh:
                parts.append(zh)
        else:
            if zh:
                parts.append(zh)
            if en:
                parts.append(en)
        if parts:
            lines = []
            for part in parts:
                lines.extend(wrap_subtitle_text(part, size, max_text_width, letter_spacing, font))
            if not lines:
                continue

            widths = [estimate_ass_text_width(line, size, letter_spacing, font) for line in lines]
            text_w = min(max(widths), max_text_width)
            line_h = max(size * 0.75, size * 1.18 + line_spacing)
            text_h = max(line_h, len(lines) * line_h)
            center_x, center_y = subtitle_anchor_center(
                sub_pos, play_res_x, play_res_y, side_margin, margin_v, text_w, text_h
            )

            if bg_enabled:
                padx = max(text_w * bg_width_pct / 200, size * 0.3) + outline_val
                pady = max(text_h * bg_height_pct / 200, size * 0.2) + outline_val
                max_bg_w = max(size, play_res_x - side_margin * 2)
                bg_w = min(text_w + 2 * padx, max_bg_w)
                bg_h = text_h + 2 * pady
                radius = bg_radius / 100 * min(bg_w, bg_h) / 2
                bg_center_x = center_x + bg_offset_x * size / 50
                bg_center_y = center_y + bg_offset_y * size / 50
                bg_left = bg_center_x - bg_w / 2
                bg_top = bg_center_y - bg_h / 2
                path = rounded_rect_ass_path_origin(bg_w, bg_h, radius)
                bg_color = hex_to_ass_bgr(bg_color_hex)
                ass += (
                    f"Dialogue: 0,{start_str},{end_str},Bg,,0,0,0,,"
                    f"{{\\an7\\pos({round(bg_left)},{round(bg_top)})\\p1\\bord0\\shad0"
                    f"\\1c&H{bg_color}&\\1a&H{bg_alpha:02X}&}}{path}\n"
                )

            horizontal = sub_pos % 3
            for line_index, line in enumerate(lines):
                line_width = min(estimate_ass_text_width(line, size, letter_spacing, font), max_text_width)
                if horizontal == 1:
                    line_x = center_x - text_w / 2 + line_width / 2
                elif horizontal == 0:
                    line_x = center_x + text_w / 2 - line_width / 2
                else:
                    line_x = center_x
                line_y = center_y - text_h / 2 + line_h / 2 + line_index * line_h
                ass += (
                    f"Dialogue: 1,{start_str},{end_str},Default,,0,0,0,,"
                    f"{{\\an5\\pos({round(line_x)},{round(line_y)})}}{line}\n"
                )

    return ass


ZH_PATTERNS = ('.zh.', '.zh-cn.', '.zh-hans.', '.zh-Hans.', '.zh-tw.', '.zh-hant.', '.zh-Hant.', '.chi.')
EN_PATTERNS = ('.en.', '.eng.', '.en-us.', '.en-gb.')


def find_subtitle_files(downloads_dir):
    """Find latest Chinese and English subtitle files."""
    zh_file = en_file = None
    for f in downloads_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in ('.srt', '.vtt'):
            continue
        name = f.name.lower()
        for pat in ZH_PATTERNS:
            if pat in name:
                if zh_file is None or f.stat().st_mtime > zh_file.stat().st_mtime:
                    zh_file = f
                break
        for pat in EN_PATTERNS:
            if pat in name:
                if en_file is None or f.stat().st_mtime > en_file.stat().st_mtime:
                    en_file = f
                break
    return zh_file, en_file


def _legacy_translate_subtitles(segments, api_url, api_key, model, update=None):
    """Translate subtitle segments to Chinese using AI."""
    if not segments or not api_url or not api_key:
        return []

    from openai import OpenAI
    client = OpenAI(base_url=api_url, api_key=api_key)

    batch_size = 80
    translated = []
    total_batches = (len(segments) + batch_size - 1) // batch_size

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        batch_num = i // batch_size + 1
        if update:
            update("downloading", 95, f"Translating subtitles ({batch_num}/{total_batches})...")

        lines = []
        for j, (start, end, text) in enumerate(batch):
            lines.append(f"{i + j + 1}. {text}")

        prompt = (
            "将以下字幕逐行翻译为简体中文。只输出翻译结果，每行格式：序号. 翻译文本。"
            "不要添加任何解释、注释或额外内容。保持序号一一对应。\n\n"
            + "\n".join(lines)
        )

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=4000,
            )
            result = resp.choices[0].message.content.strip()
            result_lines = [l.strip() for l in result.splitlines() if l.strip()]

            translations = {}
            for line in result_lines:
                dot = line.find(".")
                if dot > 0 and dot < 6:
                    try:
                        idx = int(line[:dot]) - 1
                        translations[idx] = line[dot + 1:].strip()
                    except ValueError:
                        continue

            for j, (start, end, text) in enumerate(batch):
                idx = i + j
                zh_text = translations.get(idx, "")
                translated.append((start, end, zh_text if zh_text else text))

        except Exception:
            for j, (start, end, text) in enumerate(batch):
                translated.append((start, end, text))

    return translated


def parse_numbered_translations(result, expected_indexes):
    translations = {}
    ordered = []
    for line in (result or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^\s*(\d+)\s*[\.\)、:：|]\s*(.+?)\s*$", line)
        if match:
            translations[int(match.group(1)) - 1] = match.group(2).strip()
        else:
            ordered.append(line)

    if not translations and len(ordered) == len(expected_indexes):
        for idx, line in zip(expected_indexes, ordered):
            translations[idx] = line.strip()
    return translations


def contains_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def translate_subtitles(segments, api_url, api_key, model, update=None):
    """Translate subtitle segments to Chinese using AI."""
    if not segments:
        return []
    if not api_url or not api_key:
        raise RuntimeError("没有中文字幕，需要先填写 AI API 地址和 Key，或关闭自动翻译。")

    from openai import OpenAI
    client = OpenAI(base_url=api_url.rstrip("/"), api_key=api_key)

    batch_size = 60
    translated = []
    total_batches = (len(segments) + batch_size - 1) // batch_size

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        batch_num = i // batch_size + 1
        if update:
            update("downloading", 95, f"正在翻译字幕 ({batch_num}/{total_batches})...")

        lines = []
        expected_indexes = []
        for j, (start, end, text) in enumerate(batch):
            idx = i + j
            expected_indexes.append(idx)
            lines.append(f"{idx + 1}. {text}")

        prompt = (
            "Translate the following English subtitle lines into natural Simplified Chinese.\n"
            "Keep AI/company/product terms accurate, keep each line concise for video subtitles, "
            "and output exactly one line per input.\n"
            "Output format: number. Chinese translation\n"
            "Do not add explanations, markdown, or extra text.\n\n"
            + "\n".join(lines)
        )

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a professional EN-to-ZH subtitle translator."},
                    {"role": "user", "content": prompt},
                ],
            )
            result = (resp.choices[0].message.content or "").strip()
            translations = parse_numbered_translations(result, expected_indexes)
        except Exception as exc:
            raise RuntimeError(f"AI 字幕翻译失败：{exc}") from exc

        missing = [idx for idx in expected_indexes if not translations.get(idx)]
        if missing:
            raise RuntimeError(f"AI 字幕翻译返回不完整，缺少 {len(missing)} 行。")

        for j, (start, end, text) in enumerate(batch):
            idx = i + j
            translated.append((start, end, translations[idx]))

    if not any(contains_cjk(text) for _, _, text in translated):
        raise RuntimeError("AI 字幕翻译没有返回中文内容，请检查模型或 API 设置。")

    return translated


def process_dual_subtitles(downloads_dir, opts, update=None):
    """Parse subtitle files and produce a dual ASS subtitle."""
    sub_mode = opts.get('sub_mode', 'zh_en')
    zh_file, en_file = find_subtitle_files(downloads_dir)
    if not zh_file and not en_file:
        return None

    zh_segs = parse_subtitle_file(zh_file) if zh_file else []
    en_segs = parse_subtitle_file(en_file) if en_file else []

    # Translate if no Chinese subtitles and translation is enabled
    if not zh_segs and en_segs and opts.get('translate_sub'):
        api_url = opts.get('api_url', '')
        api_key = opts.get('api_key', '')
        model = opts.get('model', 'gpt-4o-mini')
        translated = translate_subtitles(en_segs, api_url, api_key, model, update)
        if translated:
            zh_segs = translated
    elif not zh_segs and en_segs and sub_mode == 'zh_en' and update:
        update("downloading", 95, "没有找到中文字幕；如需双语，请开启自动翻译中文并填写 AI API。")

    if sub_mode == 'zh':
        en_segs = []

    if zh_segs and en_segs:
        merged = merge_subtitle_tracks(zh_segs, en_segs)
    elif zh_segs:
        merged = [[s, e, t, ''] for s, e, t in zh_segs]
    elif en_segs:
        merged = [[s, e, '', t] for s, e, t in en_segs]
    else:
        return None

    if not merged:
        return None

    video_files = sorted(
        [f for f in downloads_dir.glob("*.mp4") if ".hardsub" not in f.name],
        key=lambda x: x.stat().st_mtime, reverse=True,
    )
    if video_files:
        play_res_x, play_res_y = probe_video_dimensions(video_files[0])
        opts = dict(opts)
        opts["play_res_x"] = play_res_x
        opts["play_res_y"] = play_res_y

    ass_content = generate_dual_ass(merged, opts)
    base_name = (zh_file or en_file).stem.split('.')[0]
    ass_path = downloads_dir / f"{base_name}.dual.ass"
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(ass_content)

    return ass_path.name


def burn_subtitles_to_video(downloads_dir, ass_name, update=None):
    """Burn ASS subtitles into the video file using ffmpeg."""
    import shutil

    ass_path = downloads_dir / ass_name
    if not ass_path.exists():
        return

    video_files = sorted(
        [f for f in downloads_dir.glob("*.mp4") if ".hardsub" not in f.name],
        key=lambda x: x.stat().st_mtime, reverse=True,
    )
    if not video_files:
        return

    video = video_files[0]
    output = video.with_name(video.stem + ".hardsub.mp4")

    # Copy ASS to temp with simple name, use relative path to avoid
    # ffmpeg filter parsing issues with Windows drive letters / special chars
    tmp_dir = DOWNLOADS_DIR / "_tmp" / f"sub_burn_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_ass = tmp_dir / "_sub_burn.ass"
    try:
        shutil.copy2(str(ass_path), str(tmp_ass))
    except Exception:
        return

    try:
        if update:
            update("downloading", 99, "Burning subtitles into video...")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video),
             "-vf", "ass=_sub_burn.ass",
             "-c:a", "copy", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
             str(output)],
            capture_output=True, timeout=1800,
            cwd=str(tmp_dir),
        )
        if output.exists() and output.stat().st_size > 0:
            video.unlink()
            output.rename(video)
            # Clean up subtitle files after successful burn
            ass_path.unlink(missing_ok=True)
            base = ass_path.stem.replace('.dual', '')
            for f in downloads_dir.iterdir():
                if f.stem.startswith(base) and f.suffix.lower() in ('.vtt', '.srt', '.ass'):
                    f.unlink(missing_ok=True)
        else:
            if update:
                err = result.stderr.decode("utf-8", errors="replace")[-200:] if result.stderr else ""
                update("downloading", 99, f"Hardsub failed: {err}")
    except Exception as e:
        if output.exists():
            output.unlink(missing_ok=True)
        if update:
            update("downloading", 99, f"Hardsub error: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Local Video Subtitle Translation ─────────────────────────────────


def local_task_update(task_id, **fields):
    with local_subtitle_lock:
        task = local_subtitle_tasks.get(task_id)
        if task:
            task.update(fields)


def parse_srt_content(content):
    entries = []
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return entries
    if content.startswith("WEBVTT"):
        content = re.sub(r"^WEBVTT[^\n]*(?:\n+)", "", content, count=1).strip()
    blocks = re.split(r"\n\s*\n", content)
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        time_idx = None
        for idx, line in enumerate(lines[:2]):
            if "-->" in line:
                time_idx = idx
                break
        if time_idx is None or len(lines) <= time_idx + 1:
            continue
        try:
            start_raw, end_raw = lines[time_idx].split("-->", 1)
            entries.append({
                "index": len(entries) + 1,
                "start": parse_time(start_raw.strip()),
                "end": parse_time(end_raw.strip().split()[0]),
                "source": "\n".join(lines[time_idx + 1:]).strip(),
                "translation": "",
            })
        except Exception:
            continue
    return entries


def entries_to_srt(entries, bilingual=True, order="zh_top"):
    blocks = []
    for i, entry in enumerate(entries, 1):
        source = (entry.get("source") or "").strip()
        translation = (entry.get("translation") or "").strip()
        if bilingual and translation:
            if order == "zh_top":
                text = f"{translation}\n{source}" if source else translation
            else:
                text = f"{source}\n{translation}" if source else translation
        else:
            text = translation or source
        if not text:
            continue
        blocks.append(
            f"{i}\n"
            f"{format_srt_time(entry['start'])} --> {format_srt_time(entry['end'])}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def is_video_source_candidate(path):
    name = Path(path).name.lower()
    if not is_video_file(path):
        return False
    blocked = (".hardsub", ".translated", ".翻译字幕", ".burned", ".ae.")
    if any(mark in name for mark in blocked):
        return False
    if name.startswith("clip_") or "_clips" in name:
        return False
    return True


def newest_file(paths):
    files = [Path(path) for path in paths if Path(path).exists() and Path(path).is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def find_project_source_video(project_dir):
    candidates = []
    for folder in project_subdir_candidates(project_dir, "video"):
        if folder.exists():
            candidates.extend(p for p in folder.rglob("*") if p.is_file() and is_video_source_candidate(p))
    if not candidates:
        candidates.extend(
            p for p in Path(project_dir).rglob("*")
            if p.is_file()
            and is_video_source_candidate(p)
            and not any(part in {"30_烧录", "30_burned", "40_切片", "40_clips"} for part in p.parts)
        )
    return newest_file(candidates)


def is_translated_subtitle_candidate(path):
    path = Path(path)
    if path.suffix.lower() not in {".srt", ".vtt"}:
        return False
    name = path.name.lower()
    if any(mark in name for mark in ("raw_en", "split_en", "merged_en", "corrected_en")):
        return False
    translated_marks = ("bilingual", ".zh.", "zh-top", "en-top", "compare_zh")
    if any(mark in name for mark in translated_marks):
        return True
    try:
        return contains_cjk(path.read_text(encoding="utf-8", errors="replace")[:4000])
    except Exception:
        return False


def find_project_translated_subtitle(project_dir):
    candidates = []
    for folder in project_subdir_candidates(project_dir, "translation"):
        if folder.exists():
            candidates.extend(
                p for p in folder.rglob("*")
                if p.is_file() and is_translated_subtitle_candidate(p)
            )
    if not candidates:
        candidates.extend(
            p for p in Path(project_dir).rglob("*")
            if p.is_file()
            and is_translated_subtitle_candidate(p)
            and not any(part in {"30_烧录", "30_burned", "40_切片", "40_clips"} for part in p.parts)
        )
    return newest_file(candidates)


def resolve_burn_project_dir(session_id=""):
    if session_id:
        session = get_local_session(session_id)
        if session and session.get("project_dir"):
            project_dir = Path(session["project_dir"])
            if (
                project_dir.exists()
                and find_project_source_video(project_dir)
                and find_project_translated_subtitle(project_dir)
            ):
                return project_dir

    best = None
    best_time = -1
    for project_dir in PROJECTS_DIR.iterdir() if PROJECTS_DIR.exists() else []:
        if not project_dir.is_dir():
            continue
        video = find_project_source_video(project_dir)
        subtitle = find_project_translated_subtitle(project_dir)
        if not video or not subtitle:
            continue
        mtime = max(video.stat().st_mtime, subtitle.stat().st_mtime)
        if mtime > best_time:
            best = project_dir
            best_time = mtime
    return best


def subtitle_entries_to_burn_segments(entries, sub_mode="zh_en"):
    merged = []
    for entry in entries:
        text_parts = []
        if (entry.get("source") or "").strip():
            text_parts.extend([line.strip() for line in str(entry.get("source", "")).splitlines() if line.strip()])
        if (entry.get("translation") or "").strip():
            text_parts.extend([line.strip() for line in str(entry.get("translation", "")).splitlines() if line.strip()])
        if not text_parts:
            continue

        zh_lines = [line for line in text_parts if contains_cjk(line)]
        other_lines = [line for line in text_parts if not contains_cjk(line)]
        if zh_lines:
            zh = " ".join(zh_lines).strip()
            en = " ".join(other_lines).strip()
        else:
            zh = ""
            en = " ".join(other_lines or text_parts).strip()
        if sub_mode == "zh":
            en = ""
            if not zh:
                zh = " ".join(text_parts).strip()
        merged.append([entry["start"], entry["end"], zh, en])
    return merged


def burn_ass_to_output_video(video_path, ass_path, output_path, update=None, font_name=None):
    tmp_dir = DOWNLOADS_DIR / "_tmp" / f"youtube_burn_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_ass = tmp_dir / "_translated_burn.ass"
    shutil.copy2(str(ass_path), str(tmp_ass))
    vf_filter = "ass=_translated_burn.ass"
    font_path = resolve_font_file(font_name) if font_name else None
    if font_path:
        fonts_dir = tmp_dir / "_fonts"
        fonts_dir.mkdir(exist_ok=True)
        shutil.copy2(str(font_path), str(fonts_dir / Path(font_path).name))
        vf_filter = "ass=_translated_burn.ass:fontsdir=_fonts"
    try:
        if update:
            update(progress=70, message="正在烧录字幕到视频...")
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-vf", vf_filter,
                "-c:a", "copy",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "18",
                "-movflags", "+faststart",
                str(output_path),
            ],
            capture_output=True,
            cwd=str(tmp_dir),
            timeout=7200,
        )
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
            output_path.unlink(missing_ok=True)
            err = result.stderr.decode("utf-8", errors="replace")[-600:] if result.stderr else "ffmpeg 未返回错误信息"
            raise RuntimeError(f"烧录失败：{err}")
        return output_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_burn_translated_video_task(task_id, opts):
    try:
        local_task_update(task_id, status="running", progress=5, message="正在查找当前视频和字幕...")
        session_id = opts.get("session_id", "")
        session = get_local_session(session_id) if session_id else None
        entries = []
        subtitle = None
        subtitle_label = ""
        project_dir = None
        video = None

        if session and session.get("entries"):
            entries = [dict(entry) for entry in session.get("entries", [])]
            if session.get("project_dir"):
                project_dir = Path(session["project_dir"])
            video = resolve_session_video_path(session)
            subtitle_label = "当前工作台字幕"

        if not entries:
            project_dir = resolve_burn_project_dir(session_id)
            if not project_dir:
                raise RuntimeError("没有找到同时包含视频和字幕的项目，请先下载视频并读取/翻译字幕。")
            video = find_project_source_video(project_dir)
            subtitle = find_project_translated_subtitle(project_dir)
            if not subtitle:
                raise RuntimeError("当前项目没有找到可烧录的字幕，请先读取上传的 SRT，或导出/翻译字幕。")
            subtitle_label = subtitle.name
            srt_text = subtitle.read_text(encoding="utf-8", errors="replace")
            entries = parse_srt_content(srt_text)

        if not project_dir and session and session.get("project_dir"):
            project_dir = Path(session["project_dir"])
        if project_dir and not Path(project_dir).exists():
            project_dir = None
        if not video:
            raise RuntimeError("当前项目没有找到可烧录的源视频。")
        if not project_dir:
            project_dir = Path(video).parent

        local_task_update(
            task_id,
            progress=20,
            message=f"正在生成字幕样式：{subtitle_label}",
            project_dir=str(project_dir),
        )
        if not entries:
            raise RuntimeError("翻译字幕无法解析，请确认是 SRT/VTT 字幕文件。")
        merged = subtitle_entries_to_burn_segments(entries, opts.get("sub_mode", "zh_en"))
        if not merged:
            raise RuntimeError("翻译字幕没有可烧录的文本内容。")

        play_res_x, play_res_y = probe_video_dimensions(video)
        ass_opts = dict(opts)
        ass_opts["play_res_x"] = play_res_x
        ass_opts["play_res_y"] = play_res_y
        ass_content = generate_dual_ass(merged, ass_opts)
        burned_dir = project_subdir(project_dir, "burned")
        stem = sanitize_filename(video.stem) or "video"
        ass_path = burned_dir / f"{stem}.翻译字幕.ass"
        ass_path.write_text(ass_content, encoding="utf-8")
        output_path = burned_dir / f"{stem}.翻译字幕.mp4"
        if output_path.exists():
            output_path = burned_dir / f"{stem}.翻译字幕.{int(time.time())}.mp4"

        burn_ass_to_output_video(
            video,
            ass_path,
            output_path,
            font_name=ass_opts.get("font"),
            update=lambda **fields: local_task_update(task_id, **fields),
        )
        payload = file_download_payload(output_path)
        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"烧录完成：{output_path.name}",
            file=payload.get("file"),
            url=payload.get("url"),
            download_label="下载翻译视频",
            video=str(video),
            subtitle=str(subtitle) if subtitle else subtitle_label,
            output=str(output_path),
            project_dir=str(project_dir),
        )
    except Exception as e:
        local_task_update(task_id, status="error", message=str(e), error=str(e))


def serialize_local_entries(entries, limit=120):
    visible = entries[:limit] if limit else entries
    return [{
        "index": i + 1,
        "start": format_srt_time(e["start"]),
        "end": format_srt_time(e["end"]),
        "source": e.get("source", ""),
        "translation": e.get("translation", ""),
        "translation_compare": e.get("translation_compare", ""),
    } for i, e in enumerate(visible)]


def create_local_session(original_name, entries, project_dir=None, translation_dir=None, source_path=None, translation_model=""):
    session_id = str(uuid.uuid4())[:8]
    for i, entry in enumerate(entries, 1):
        entry["index"] = i
    with local_subtitle_lock:
        local_subtitle_sessions[session_id] = {
            "id": session_id,
            "name": original_name,
            "entries": entries,
            "corrections": [],
            "project_dir": str(project_dir) if project_dir else "",
            "translation_dir": str(translation_dir) if translation_dir else "",
            "source_path": str(source_path) if source_path else "",
            "reference_materials": "",
            "translation_reference": "",
            "translation_model": translation_model or "",
            "created_at": time.time(),
        }
    return session_id


def get_local_session(session_id):
    with local_subtitle_lock:
        session = local_subtitle_sessions.get(session_id)
    if not session:
        return None
    return session


def local_session_payload(session, limit=120):
    entries = session.get("entries", [])
    translated = sum(1 for e in entries if (e.get("translation") or "").strip())
    compared = sum(1 for e in entries if (e.get("translation_compare") or "").strip())
    return {
        "session_id": session["id"],
        "name": session.get("name", ""),
        "segments": len(entries),
        "translated": translated,
        "compared": compared,
        "corrections": session.get("corrections", []),
        "project_dir": session.get("project_dir", ""),
        "translation_dir": session.get("translation_dir", ""),
        "reference_materials": session.get("reference_materials", ""),
        "translation_reference": session.get("translation_reference", ""),
        "translation_model": session.get("translation_model", ""),
        "entries": serialize_local_entries(entries, limit=limit),
    }


def write_translation_reference_file(translation_dir, reference_text, materials="", title=""):
    reference_text = (reference_text or "").strip()
    if not translation_dir or not reference_text:
        return None
    output_dir = Path(translation_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "translation_reference.md"
    parts = ["# 翻译参考资料"]
    if title:
        parts.append(f"项目：{title}")
    parts.append(f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    parts.append("")
    parts.append(reference_text)
    if (materials or "").strip():
        parts.append("")
        parts.append("## 用户提供资料")
        parts.append((materials or "").strip())
    output_path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")
    return output_path


def set_session_translation_reference(session_id, reference_text="", materials=""):
    reference_text = (reference_text or "").strip()
    materials = (materials or "").strip()
    if not session_id:
        return None
    with local_subtitle_lock:
        session = local_subtitle_sessions.get(session_id)
        if not session:
            return None
        session["reference_materials"] = materials
        session["translation_reference"] = reference_text
        session_copy = dict(session)
    if not reference_text:
        return None
    return write_translation_reference_file(
        session_copy.get("translation_dir", ""),
        reference_text,
        materials,
        session_copy.get("name", ""),
    )


def text_is_cjk(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def local_text_len(text):
    if text_is_cjk(text):
        return len(re.findall(r"[\u4e00-\u9fff]", text))
    return len(re.findall(r"[A-Za-z0-9]+", text))


def merge_local_short_entries(entries, max_chars_cjk=30, max_words_latin=14, max_gap=0.5):
    merged = []
    changed = 0
    i = 0
    while i < len(entries):
        cur = dict(entries[i])
        while i + 1 < len(entries):
            nxt = entries[i + 1]
            gap = nxt["start"] - cur["end"]
            if gap > max_gap:
                break
            cur_limit = max_chars_cjk if text_is_cjk(cur.get("source", "")) else max_words_latin
            next_limit = max_chars_cjk if text_is_cjk(nxt.get("source", "")) else max_words_latin
            limit = max(cur_limit, next_limit)
            cur_len = local_text_len(cur.get("source", ""))
            combined_source = (cur.get("source", "").strip() + " " + nxt.get("source", "").strip()).strip()
            combined_translation = (
                cur.get("translation", "").strip() + " " + nxt.get("translation", "").strip()
            ).strip()
            if cur_len >= limit or local_text_len(combined_source) > limit * 1.6:
                break
            cur["end"] = nxt["end"]
            cur["source"] = combined_source
            cur["translation"] = combined_translation
            i += 1
            changed += 1
        merged.append(cur)
        i += 1

    for idx, entry in enumerate(merged, 1):
        entry["index"] = idx
    return merged, changed


def semantic_cut_position(text, max_chars, min_chars):
    limit = min(max_chars, len(text) - 1)
    if limit <= min_chars:
        return limit
    head = text[:limit + 1]

    if text_is_cjk(text):
        punctuation = list(re.finditer(r"[。！？；，、](?:\s*)", head))
        punctuation = [m for m in punctuation if m.end() >= min_chars]
        if punctuation:
            return punctuation[-1].end()
    else:
        punctuation = list(re.finditer(r"[,;:.!?](?:\s+|$)", head))
        punctuation = [m for m in punctuation if m.end() >= min_chars]
        if punctuation:
            return punctuation[-1].end()

        connectors = (
            r"\s+(?=(?:and|but|because|so|until|when|while|which|that|where|if|"
            r"though|although|since|as|after|before|unless|rather|instead)\b)"
        )
        matches = list(re.finditer(connectors, head, flags=re.IGNORECASE))
        matches = [m for m in matches if m.start() >= min_chars]
        if matches:
            return matches[-1].start()

    space = head.rfind(" ")
    if space >= min_chars:
        return space
    return limit


def split_text_semantic(text, max_chars, min_chars):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text or len(text) <= max_chars:
        return [text] if text else []

    parts = []
    rest = text
    while len(rest) > max_chars:
        cut = semantic_cut_position(rest, max_chars, min_chars)
        chunk = rest[:cut].strip()
        rest = rest[cut:].strip()
        rest = re.sub(r"^[,;，、]\s*", "", rest)
        if chunk:
            parts.append(chunk)
        if not rest:
            break

    if rest:
        if parts and len(rest) < min_chars // 2 and len(parts[-1]) + 1 + len(rest) <= int(max_chars * 1.25):
            parts[-1] = f"{parts[-1]} {rest}".strip()
        else:
            parts.append(rest)
    return parts


def split_long_subtitle_entries(entries, max_chars_latin=84, max_chars_cjk=42):
    result = []
    split_count = 0
    cleared_translations = 0

    for entry in entries:
        source = re.sub(r"\s+", " ", (entry.get("source") or "").strip())
        if not source:
            result.append(dict(entry))
            continue

        is_cjk = text_is_cjk(source)
        max_chars = max_chars_cjk if is_cjk else max_chars_latin
        min_chars = 16 if is_cjk else 32
        parts = split_text_semantic(source, max_chars=max_chars, min_chars=min_chars)
        if len(parts) <= 1:
            result.append(dict(entry, source=source))
            continue

        split_count += len(parts) - 1
        duration = max(0.01, float(entry["end"]) - float(entry["start"]))
        weights = [max(1, len(re.sub(r"\s+", "", part))) for part in parts]
        total = max(1, sum(weights))
        cursor = float(entry["start"])
        translation = (entry.get("translation") or "").strip()
        if translation:
            cleared_translations += 1

        for idx, part in enumerate(parts):
            if idx == len(parts) - 1:
                end = float(entry["end"])
            else:
                end = cursor + duration * weights[idx] / total
            if end <= cursor:
                end = min(float(entry["end"]), cursor + 0.5)
            result.append({
                "index": len(result) + 1,
                "start": cursor,
                "end": end,
                "source": part,
                "translation": "",
            })
            cursor = end

    for idx, item in enumerate(result, 1):
        item["index"] = idx
    return result, split_count, cleared_translations


def is_video_file(path):
    return Path(path).suffix.lower() in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")


def parse_local_filesystem_path(raw_path):
    raw = (raw_path or "").strip().strip('"').strip("'")
    if raw.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        path_text = urllib.parse.unquote(parsed.path or "")
        if parsed.netloc:
            path_text = f"//{parsed.netloc}{path_text}"
        if re.match(r"^/[A-Za-z]:", path_text):
            path_text = path_text[1:]
        raw = path_text.replace("/", "\\")
    return Path(raw).expanduser()


def find_primary_video_in_directory(directory):
    directory = Path(directory)
    candidates = [
        path for path in directory.rglob("*")
        if path.is_file() and is_video_file(path)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def pick_windows_local_path(kind="file"):
    if os.name != "nt":
        raise RuntimeError("本地路径选择器目前只支持 Windows。")
    if kind == "directory":
        script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择包含视频的目录'
$dialog.ShowNewFolderButton = $false
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::WriteLine($dialog.SelectedPath)
}
"""
    else:
        script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择本地视频文件'
$dialog.Filter = 'Video files (*.mp4;*.mov;*.mkv;*.webm;*.m4v;*.avi)|*.mp4;*.mov;*.mkv;*.webm;*.m4v;*.avi|All files (*.*)|*.*'
$dialog.Multiselect = $false
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::WriteLine($dialog.FileName)
}
"""
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "路径选择窗口打开失败").strip())
    return result.stdout.strip()


def resolve_session_video_path(session):
    source_path = Path(session.get("source_path") or "")
    if source_path.exists() and is_video_file(source_path):
        return source_path
    project_dir = session.get("project_dir")
    if project_dir:
        video = find_primary_video(Path(project_dir))
        if video and video.exists():
            return video
    return None


def default_original_clip_dir(video_path):
    video_path = Path(video_path)
    safe_stem = sanitize_filename(video_path.stem).strip() or "video"
    return video_path.parent / f"{safe_stem}_clips"


def resolve_clip_output_dir(session, video_path):
    clip_output_dir = session.get("clip_output_dir")
    if clip_output_dir:
        out_dir = Path(clip_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir
    project_dir = session.get("project_dir")
    if project_dir:
        _, subdirs = ensure_project_dirs(Path(project_dir).name)
        return subdirs["clips"]
    out_dir = default_original_clip_dir(video_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def clip_entries_for_range(entries, start, end):
    clipped = []
    for entry in entries:
        entry_start = float(entry.get("start", 0))
        entry_end = float(entry.get("end", 0))
        if entry_end <= start or entry_start >= end:
            continue
        clipped.append({
            "index": len(clipped) + 1,
            "start": max(0, entry_start - start),
            "end": max(0.01, min(entry_end, end) - start),
            "source": entry.get("source", ""),
            "translation": entry.get("translation", ""),
        })
    return clipped


def run_local_clip_task(task_id, session_id, clips):
    try:
        session = get_local_session(session_id)
        if not session:
            raise RuntimeError("字幕会话不存在，请先读取上传的视频。")
        video_path = resolve_session_video_path(session)
        if not video_path:
            raise RuntimeError("当前会话没有可切片的视频，请先上传并读取视频文件。")

        clips_dir = resolve_clip_output_dir(session, video_path)
        clips_dir.mkdir(parents=True, exist_ok=True)
        total = len(clips)
        if total <= 0:
            raise RuntimeError("请至少添加一个切片时间段。")

        manifest = []
        for index, clip in enumerate(clips, 1):
            title = sanitize_filename((clip.get("title") or f"clip_{index:02d}").strip())
            title = title[:60] or f"clip_{index:02d}"
            start = parse_clip_time(clip.get("start"))
            end = parse_clip_time(clip.get("end"))
            if end <= start:
                raise RuntimeError(f"第 {index} 个切片结束时间必须大于开始时间。")
            duration = end - start
            if duration < 1:
                raise RuntimeError(f"第 {index} 个切片太短，请至少保留 1 秒。")

            stem = f"clip_{index:02d}_{title}"
            output_video = clips_dir / f"{stem}.mp4"
            output_srt = clips_dir / f"{stem}.srt"
            local_task_update(
                task_id,
                status="running",
                progress=round((index - 1) / total * 90),
                message=f"正在导出切片 {index}/{total}...",
            )
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(duration),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(output_video),
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
            )
            if result.returncode != 0 or not output_video.exists() or output_video.stat().st_size <= 0:
                detail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
                raise RuntimeError(f"第 {index} 个切片导出失败：" + "；".join(detail))

            clip_entries = clip_entries_for_range(session.get("entries", []), start, end)
            if clip_entries:
                output_srt.write_text(entries_to_srt(clip_entries, bilingual=True), encoding="utf-8")

            manifest.append({
                "index": index,
                "title": clip.get("title") or f"clip_{index:02d}",
                "start": clip_time_label(start),
                "end": clip_time_label(end),
                "duration": round(duration, 3),
                "video": path_download_info(output_video),
                "subtitle": path_download_info(output_srt) if output_srt.exists() else {},
            })

        manifest_path = clips_dir / "manual_clips.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"切片完成：导出 {len(manifest)} 个片段到 {clips_dir}",
            clips=manifest,
            **path_download_info(manifest_path),
        )
    except Exception as exc:
        local_task_update(task_id, status="error", message=str(exc), error=str(exc))


def strip_code_fence(text):
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def extract_json_array(text):
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except Exception:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end + 1])
                return data if isinstance(data, list) else []
            except Exception:
                return []
    return []


def extract_json_object(text):
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {}
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end + 1])
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
    return {}


def normalize_api_key(api_key):
    key = (api_key or "").strip()
    return re.sub(r"^Bearer\s+", "", key, flags=re.IGNORECASE).strip()


def call_chat_model(api_url, api_key, model, messages):
    from openai import OpenAI

    client = OpenAI(base_url=api_url.rstrip("/"), api_key=normalize_api_key(api_key))
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
    except Exception as exc:
        text = str(exc)
        lowered = text.lower()
        if (
            "401" in text
            or "invalid api key" in lowered
            or "unauthorized" in lowered
            or "无效的 api key" in lowered
            or "无效的 API Key" in text
        ):
            raise RuntimeError(
                "翻译/AI 检查使用的大模型 API Key 无效。"
                "火山 API Key 只用于语音识别，请在下方 AI 设置里填写翻译模型的 API 地址、Key 和模型名。"
            ) from exc
        raise RuntimeError(f"调用翻译/AI 检查模型失败：{text}") from exc
    return (response.choices[0].message.content or "").strip()


def subtitle_text_sample(entries, limit=240, max_chars=18000):
    lines = []
    for entry in entries[:limit]:
        text = (entry.get("source") or "").replace("\n", " ").strip()
        if text:
            lines.append(f"{entry.get('index', len(lines) + 1)} | {text}")
    return "\n".join(lines)[:max_chars]


def build_translation_reference(entries, api_url, api_key, model, materials="", user_hint="", entity_profile_boost=True):
    sample = subtitle_text_sample(entries)
    material_text = (materials or "").strip()[:20000]
    hint = (user_hint or "").strip()
    no_subtitle_mode = not bool(sample.strip())
    task_mode = (
        "当前没有字幕样本。请把用户输入的视频标题、链接、人物、公司、产品、主题或关键词当作检索线索，先为后续字幕翻译准备参考资料。"
        if no_subtitle_mode
        else "当前已有字幕样本。请结合字幕样本和用户资料，为后续字幕校正与翻译准备参考资料。"
    )
    entity_rules = ""
    if entity_profile_boost:
        entity_rules = """人物/专名背景增强：
- 请重点识别并整理人名、公司名、产品名、AI 模型名、机构名、节目名、论文/项目名和缩写。
- 如果可以联网，请优先搜索人物的职业身份、所在公司、职位、代表项目，以及他们在本视频主题中的相关背景。
- 人名、公司名、产品名、模型名默认保留英文；不要强行音译。
- 对每个关键人物尽量给出：英文名、身份/职位、相关公司/产品、翻译时需要注意的上下文。
- 如果身份不确定，必须写入“待确认”，不要编造。"""

    prompt = f"""你是字幕翻译前的资料研究与整理助手。你的目标不是翻译字幕，而是生成后续字幕翻译会用到的“翻译参考表”。

任务模式：
{task_mode}

联网规则：
- 如果当前模型/API 支持联网搜索，请围绕用户提供的链接、标题、关键词、人物、公司、产品名和主题检索公开信息，并把有把握的信息整理进参考表。
- 如果当前模型/API 不支持联网搜索，不要假装已经搜索；只基于用户输入和已有知识整理，并在“建议搜索关键词”和“待确认”里列出需要用户或联网模型补充的信息。
- 不要编造具体事实、职位、日期、融资金额、产品细节；不确定就写“待确认”。
{entity_rules}

输出要求：
- 使用简体中文。
- 不要翻译整段字幕，只整理参考。
- 优先覆盖 AI、技术、投资、公司、人名、产品名、模型名、缩写词、专有名词。
- 专属名称、人名、公司名、产品名、模型名默认保留英文；必要时给中文解释，不强行音译。
- 给出中英文术语对照、统一译法、ASR 易错词、翻译风格规则。
- 内容要紧凑，适合每次翻译字幕时放进提示词。

用户输入/背景提示：
{hint or "无"}

用户提供的参考资料、链接或关键词：
{material_text or "无"}

字幕样本：
{sample or "无"}

请按下面结构输出：
## 内容背景
## 建议搜索关键词
## 人物身份/职业背景（默认保留英文名）
## 公司/产品/模型名（默认保留英文）
## 术语与统一译法
## ASR 易错词校正
## 翻译风格规则
## 待确认
"""
    return call_chat_model(api_url, api_key, model, [{"role": "user", "content": prompt}]).strip()


def analyze_asr_corrections(entries, api_url, api_key, model, user_hint=""):
    sample = "\n".join(e["source"] for e in entries[:220])
    sample = sample[:16000]
    hint = user_hint.strip() or "视频内容可能涉及 AI、编程、创业、产品名、公司名和人名。"
    prompt = f"""你是专业的 ASR 字幕校正助手。请从下面的英文字幕中找出明显的语音识别误识别词，尤其是产品名、人名、公司名、技术术语和缩写词。

背景提示：
{hint}

待分析字幕：
{sample}

只输出 JSON 数组，不要 Markdown。每个元素格式：
{{"wrong":"字幕中实际出现的错误文本","correct":"应替换为的正确文本","reason":"简短原因"}}

只给高把握建议；如果没有明显错误，输出 []。"""
    messages = [
        {"role": "system", "content": "You output only valid JSON."},
        {"role": "user", "content": prompt},
    ]
    try:
        return extract_json_array(call_chat_model(api_url, api_key, model, messages))
    except Exception:
        return []


def apply_asr_corrections(entries, corrections):
    applied = 0
    normalized = []
    for item in corrections:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong", "")).strip()
        correct = str(item.get("correct", "")).strip()
        if wrong and correct and wrong != correct:
            normalized.append((wrong, correct))

    for entry in entries:
        text = entry["source"]
        for wrong, correct in normalized:
            if wrong in text:
                count = text.count(wrong)
                text = text.replace(wrong, correct)
                applied += count
            else:
                text, count = re.subn(re.escape(wrong), correct, text, flags=re.IGNORECASE)
                applied += count
        entry["source"] = text
    return applied


CHARACTER_CN_NAME_RULE = """知名角色名中英对照规则：
- 仅针对影视、动画、文学、游戏等作品中的知名角色名；真人姓名、公司名、产品名、模型名和机构名仍按专名规则处理。
- 翻译中文字幕时，遇到有通用中文译名的知名角色名，保留英文原名，并在后面加括号写通用中文译名，例如 Nemo（尼莫）、Dory（多莉）、Woody（胡迪）、Bambi（小鹿斑比）、Michael Corleone（迈克尔·柯里昂）。
- 如果背景提示或翻译参考表提供了统一译法，优先使用参考资料；如果不确定通用中文译名，不要编造，只保留英文原名。
- 不要把已经是中文作品名或普通人名的内容强行改成角色中英对照。"""


def truthy_value(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def build_translation_prompt_hint(user_hint="", character_cn_names=False):
    hint = (user_hint or "").strip()
    if truthy_value(character_cn_names) and "知名角色名中英对照规则" not in hint:
        hint = f"{hint}\n\n{CHARACTER_CN_NAME_RULE}" if hint else CHARACTER_CN_NAME_RULE
    return hint


def translate_local_entries(task_id, entries, api_url, api_key, model, user_hint=""):
    batch_size = 60
    total_batches = max(1, (len(entries) + batch_size - 1) // batch_size)
    hint = user_hint.strip()

    for offset in range(0, len(entries), batch_size):
        batch = entries[offset:offset + batch_size]
        batch_no = offset // batch_size + 1
        local_task_update(
            task_id,
            status="running",
            progress=60 + round(batch_no / total_batches * 30),
            message=f"正在翻译并微调字幕 {batch_no}/{total_batches}...",
        )
        lines = "\n".join(f"{e['index']} | {e['source'].replace(chr(10), ' ')}" for e in batch)
        prompt = f"""你是专业字幕翻译和 ASR 校正助手。下面是从本地视频识别出的字幕。
请逐行完成两件事：
1. 轻微修正明显的英文 ASR 错词，不要改写风格，不要合并或拆分字幕。
2. 翻译为自然、简洁的简体中文，适合视频字幕。
3. 人名、公司名、产品名、模型名、机构名、节目名、论文/项目名和缩写默认保留英文；不要强行音译。
4. 如果背景提示或翻译参考表提供了人物身份、职业背景、公司职位或产品背景，翻译时用这些信息理解上下文；必要时可在中文中简短补充身份，但不要添加英文没有的信息。
5. 对不确定的人名或术语，保留英文，不要编造身份。

背景提示：
{hint or "无"}

输入格式：
序号 | 英文原文

输出要求：
- 只输出逐行结果，不要解释。
- 每行严格使用：序号 | 修正后的英文 | 中文译文
- 输入多少行，输出多少行，序号必须一致。

待处理字幕：
{lines}"""
        messages = [{"role": "user", "content": prompt}]
        result = call_chat_model(api_url, api_key, model, messages)

        by_index = {}
        ordered = []
        for line in result.splitlines():
            match = re.match(r"^\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*$", line)
            if not match:
                continue
            idx = int(match.group(1))
            source = match.group(2).strip()
            zh = match.group(3).strip()
            by_index[idx] = (source, zh)
            ordered.append((source, zh))

        for i, entry in enumerate(batch):
            source, zh = by_index.get(entry["index"], ordered[i] if i < len(ordered) else ("", ""))
            if source:
                entry["source"] = source
            entry["translation"] = zh or entry["source"]


def translate_compare_entries(task_id, entries, api_url, api_key, model, user_hint=""):
    batch_size = 60
    total_batches = max(1, (len(entries) + batch_size - 1) // batch_size)
    hint = user_hint.strip()

    for offset in range(0, len(entries), batch_size):
        batch = entries[offset:offset + batch_size]
        batch_no = offset // batch_size + 1
        local_task_update(
            task_id,
            status="running",
            progress=88 + round(batch_no / total_batches * 8),
            message=f"正在生成对比翻译 {batch_no}/{total_batches}...",
        )
        lines = "\n".join(f"{e['index']} | {e['source'].replace(chr(10), ' ')}" for e in batch)
        prompt = f"""你是专业英文到简体中文字幕翻译助手。请把下面字幕逐行翻译成自然、简洁的中文，保留 AI、公司、产品和人名术语准确。
不要修改英文原文，不要合并或拆分字幕。
人名、公司名、产品名、模型名、机构名、节目名、论文/项目名和缩写默认保留英文，不要强行音译。
如果背景提示或翻译参考表提供了人物身份、职业背景、公司职位或产品背景，翻译时用这些信息理解上下文；不确定就保留英文，不要编造身份。

背景提示：
{hint or "无"}

输入格式：
序号 | 英文原文

输出要求：
- 只输出逐行结果，不要解释。
- 每行严格使用：序号 | 中文译文
- 输入多少行，输出多少行，序号必须一致。

待翻译字幕：
{lines}"""
        result = call_chat_model(api_url, api_key, model, [{"role": "user", "content": prompt}])

        by_index = {}
        ordered = []
        for line in result.splitlines():
            match = re.match(r"^\s*(\d+)\s*\|\s*(.*?)\s*$", line)
            if not match:
                continue
            idx = int(match.group(1))
            zh = match.group(2).strip()
            by_index[idx] = zh
            ordered.append(zh)

        for i, entry in enumerate(batch):
            zh = by_index.get(entry["index"], ordered[i] if i < len(ordered) else "")
            entry["translation_compare"] = zh


VOLC_FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
VOLC_STANDARD_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
VOLC_STANDARD_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
VOLC_SUCCESS_CODE = "20000000"
VOLC_WAITING_CODES = {"20000001", "20000002", "20000003"}
ONLINE_ASR_PROVIDERS = {"volc_flash", "volc_standard_url"}


def normalize_online_asr_provider(provider):
    provider = (provider or "volc_flash").strip()
    return provider if provider in ONLINE_ASR_PROVIDERS else "volc_flash"


def volc_language_code(language):
    return {
        "en": "en-US",
        "zh": "zh-CN",
        "ja": "ja-JP",
        "ko": "ko-KR",
        "auto": "",
    }.get((language or "en").lower(), language or "en-US")


def volc_json_request(url, payload, headers, timeout=120):
    req_headers = {"Content-Type": "application/json", **headers}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip() else {}
            return dict(resp.headers), parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = {"message": raw}
        parsed_dict = parsed if isinstance(parsed, dict) else {}
        status_code = exc.headers.get("X-Api-Status-Code") or parsed_dict.get("code") or exc.code
        message = (
            exc.headers.get("X-Api-Message")
            or parsed_dict.get("message")
            or parsed_dict.get("error")
            or raw
        )
        raise RuntimeError(f"火山识别请求失败：{status_code} {message}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接火山识别接口：{exc.reason}")


def volc_status_code(headers, body):
    body_dict = body if isinstance(body, dict) else {}
    return str(
        headers.get("X-Api-Status-Code")
        or headers.get("x-api-status-code")
        or body_dict.get("code")
        or body_dict.get("status_code")
        or ""
    )


def volc_status_message(headers, body):
    body_dict = body if isinstance(body, dict) else {}
    return (
        headers.get("X-Api-Message")
        or headers.get("x-api-message")
        or body_dict.get("message")
        or body_dict.get("msg")
        or body_dict.get("error")
        or ""
    )


def first_present(obj, *keys):
    for key in keys:
        if isinstance(obj, dict) and key in obj and obj[key] is not None:
            return obj[key]
    return None


def volc_extract_text(body):
    if not isinstance(body, dict):
        return ""
    result = body.get("result", body)
    if isinstance(result, dict):
        return result.get("text") or result.get("transcript") or body.get("text") or ""
    if isinstance(result, list):
        return " ".join(
            (item.get("text") or item.get("transcript") or "").strip()
            for item in result
            if isinstance(item, dict)
        ).strip()
    return body.get("text") or ""


def volc_extract_entries(body, offset=0.0, fallback_duration=0.0):
    utterances = []

    def collect(node):
        if isinstance(node, dict):
            value = node.get("utterances") or node.get("segments")
            if isinstance(value, list):
                utterances.extend(item for item in value if isinstance(item, dict))
                return
            for key in ("result", "results", "data"):
                if key in node:
                    collect(node[key])
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(body)
    entries = []
    for item in utterances:
        text = (item.get("text") or item.get("utterance") or item.get("sentence") or "").strip()
        if not text:
            continue
        start_raw = first_present(item, "start_time", "start", "begin_time", "begin")
        end_raw = first_present(item, "end_time", "end", "finish_time", "finish")
        try:
            start = float(start_raw) / 1000.0 if start_raw is not None else 0.0
            end = float(end_raw) / 1000.0 if end_raw is not None else start + 3.0
        except Exception:
            start, end = 0.0, 3.0
        if end <= start:
            end = start + 3.0
        entries.append({
            "index": len(entries) + 1,
            "start": offset + start,
            "end": offset + end,
            "source": text,
            "translation": "",
        })

    if entries:
        return entries

    text = volc_extract_text(body).strip()
    if text:
        return [{
            "index": 1,
            "start": offset,
            "end": offset + max(float(fallback_duration or 3), 3.0),
            "source": text,
            "translation": "",
        }]
    return []


def probe_media_duration(media_path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return max(0.0, float(result.stdout.strip().splitlines()[0]))
    except Exception:
        pass
    return 0.0


def prepare_online_asr_audio(task_id, media_path, opts):
    media_path = Path(media_path)
    suffix = media_path.suffix.lower()
    project_dir = Path(opts["project_dir"]) if opts.get("project_dir") else None
    if project_dir:
        _, subdirs = ensure_project_dirs(project_dir.name)
        audio_dir = subdirs["audio"]
    else:
        audio_dir = media_path.parent
    audio_dir.mkdir(parents=True, exist_ok=True)

    if suffix == ".mp3" and media_path.stat().st_size <= 90 * 1024 * 1024:
        return media_path

    target = audio_dir / f"{sanitize_filename(media_path.stem)}.asr.mp3"
    if target.exists() and target.stat().st_size > 0:
        return target

    local_task_update(task_id, status="running", progress=6, message="正在准备在线识别用 MP3 音频...")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(media_path),
            "-map", "0:a:0", "-vn",
            "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "16000", "-ac", "1",
            "-id3v2_version", "3", "-write_id3v1", "1",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError("准备在线识别音频失败，请确认 FFmpeg 可用且文件包含音轨。")
    return target


def split_audio_for_volc_flash(task_id, audio_path, segment_seconds=1500):
    audio_path = Path(audio_path)
    duration = probe_media_duration(audio_path)
    max_bytes = 90 * 1024 * 1024
    if (duration and duration <= segment_seconds) and audio_path.stat().st_size <= max_bytes:
        return [(audio_path, 0.0, duration)]
    if not duration and audio_path.stat().st_size <= max_bytes:
        return [(audio_path, 0.0, 0.0)]
    if not duration:
        raise RuntimeError("无法读取音频时长，不能安全分段在线识别。请先转成标准 MP3 后再试。")

    chunk_dir = audio_path.parent / f"{sanitize_filename(audio_path.stem)}_volc_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    start = 0.0
    index = 0
    while start < duration:
        chunk = chunk_dir / f"chunk_{index:03d}.mp3"
        local_task_update(
            task_id,
            status="running",
            progress=min(18, 8 + round(start / max(duration, 1) * 10)),
            message=f"正在切分在线识别音频 {index + 1}...",
        )
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(round(start, 3)),
                "-i", str(audio_path),
                "-t", str(segment_seconds),
                "-vn", "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "16000", "-ac", "1",
                "-id3v2_version", "3", "-write_id3v1", "1",
                str(chunk),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
        if result.returncode != 0 or not chunk.exists() or chunk.stat().st_size <= 0:
            raise RuntimeError("切分在线识别音频失败，请检查 FFmpeg。")
        chunks.append((chunk, start, min(segment_seconds, duration - start)))
        start += segment_seconds
        index += 1
    return chunks


def volc_flash_transcribe_chunk(chunk_path, opts):
    api_key = (opts.get("volc_api_key") or "").strip()
    resource_id = (opts.get("volc_resource_id") or "").strip() or "volc.bigasr.auc_turbo"
    if not api_key:
        raise RuntimeError("请先填写火山 API Key。")

    language = volc_language_code(opts.get("language", "en"))
    audio_payload = {
        "data": base64.b64encode(Path(chunk_path).read_bytes()).decode("ascii"),
        "format": "mp3",
    }
    if language:
        audio_payload["language"] = language

    payload = {
        "user": {"uid": "youtube-local-subtitle"},
        "audio": audio_payload,
        "request": {
            "model_name": "bigmodel",
            "show_utterances": True,
            "enable_punc": True,
            "enable_itn": True,
        },
    }
    headers, body = volc_json_request(
        VOLC_FLASH_URL,
        payload,
        {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
        },
        timeout=300,
    )
    code = volc_status_code(headers, body)
    if code and code != VOLC_SUCCESS_CODE:
        message = volc_status_message(headers, body) or json.dumps(body, ensure_ascii=False)[:300]
        raise RuntimeError(f"火山在线识别失败：{code} {message}")
    return body


def transcribe_volc_flash_media(task_id, media_path, opts):
    local_task_update(task_id, status="running", progress=4, message="正在准备火山在线识别...")
    audio_path = prepare_online_asr_audio(task_id, media_path, opts)
    segment_seconds = int(float(opts.get("volc_segment_minutes") or 25) * 60)
    segment_seconds = max(300, min(segment_seconds, 3600))
    chunks = split_audio_for_volc_flash(task_id, audio_path, segment_seconds=segment_seconds)

    entries = []
    for i, (chunk_path, offset, chunk_duration) in enumerate(chunks, 1):
        local_task_update(
            task_id,
            status="running",
            progress=18 + round(i / max(len(chunks), 1) * 37),
            message=f"火山在线识别中 {i}/{len(chunks)}...",
        )
        body = volc_flash_transcribe_chunk(chunk_path, opts)
        entries.extend(volc_extract_entries(body, offset=offset, fallback_duration=chunk_duration))

    if not entries:
        raise RuntimeError("火山在线识别完成，但没有返回可用字幕。")
    entries.sort(key=lambda item: (item["start"], item["end"]))
    for index, entry in enumerate(entries, 1):
        entry["index"] = index
    return entries_to_srt(entries, bilingual=False, order="en_top")


def transcribe_volc_standard_url(task_id, opts):
    api_key = (opts.get("volc_api_key") or "").strip()
    resource_id = (opts.get("volc_resource_id") or "").strip() or "volc.bigasr.auc"
    audio_url = (opts.get("volc_audio_url") or "").strip()
    if not api_key:
        raise RuntimeError("请先填写火山 API Key。")
    if not audio_url:
        raise RuntimeError("火山标准版需要可公网访问的音频 URL；本地文件直传请选“火山极速版”。")

    request_id = str(uuid.uuid4())
    language = volc_language_code(opts.get("language", "en"))
    payload = {
        "user": {"uid": "youtube-local-subtitle"},
        "audio": {"url": audio_url, "format": "mp3"},
        "request": {
            "model_name": "bigmodel",
            "show_utterances": True,
            "enable_punc": True,
            "enable_itn": True,
        },
    }
    if language:
        payload["audio"]["language"] = language

    local_task_update(task_id, status="running", progress=8, message="正在提交火山标准版识别任务...")
    headers, body = volc_json_request(
        VOLC_STANDARD_SUBMIT_URL,
        payload,
        {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        },
        timeout=120,
    )
    code = volc_status_code(headers, body)
    if code and code != VOLC_SUCCESS_CODE:
        raise RuntimeError(f"火山标准版提交失败：{code} {volc_status_message(headers, body)}")

    for poll in range(1, 721):
        time.sleep(5)
        local_task_update(
            task_id,
            status="running",
            progress=min(55, 10 + poll // 4),
            message=f"正在查询火山标准版识别结果 {poll}...",
        )
        headers, body = volc_json_request(
            VOLC_STANDARD_QUERY_URL,
            {},
            {
                "X-Api-Key": api_key,
                "X-Api-Resource-Id": resource_id,
                "X-Api-Request-Id": request_id,
                "X-Api-Sequence": "-1",
            },
            timeout=120,
        )
        code = volc_status_code(headers, body)
        if code == VOLC_SUCCESS_CODE or not code:
            entries = volc_extract_entries(body)
            if entries:
                return entries_to_srt(entries, bilingual=False, order="en_top")
        if code not in VOLC_WAITING_CODES:
            raise RuntimeError(f"火山标准版查询失败：{code} {volc_status_message(headers, body)}")
    raise RuntimeError("火山标准版识别超时，请稍后在控制台确认任务状态。")


def transcribe_media(task_id, media_path, opts):
    provider = normalize_online_asr_provider(opts.get("asr_provider"))
    if provider == "volc_flash":
        return transcribe_volc_flash_media(task_id, media_path, opts)
    if provider == "volc_standard_url":
        return transcribe_volc_standard_url(task_id, opts)
    raise RuntimeError("请选择火山极速版或火山标准版 URL。")


def run_local_subtitle_task(task_id, media_path, original_name, opts):
    try:
        local_task_update(task_id, status="running", progress=2, message="正在准备在线识别...")
        srt_text = transcribe_media(task_id, media_path, opts)
        entries = parse_srt_content(srt_text)
        if not entries:
            raise RuntimeError("未识别出可用字幕。")
        project_dir = Path(opts["project_dir"]) if opts.get("project_dir") else None
        translation_dir = Path(opts["translation_dir"]) if opts.get("translation_dir") else None
        if translation_dir:
            translation_dir.mkdir(parents=True, exist_ok=True)
            (translation_dir / "raw_en.srt").write_text(srt_text, encoding="utf-8")
        if project_dir:
            _, subdirs = ensure_project_dirs(project_dir.name)
            media_stem = local_media_output_stem(original_name, "local_video")
            (subdirs["subtitles"] / f"{media_stem}.raw_en.srt").write_text(
                srt_text,
                encoding="utf-8",
            )

        local_task_update(task_id, progress=55, message="正在合并短句并智能分段...")
        entries, merged_count = merge_local_short_entries(entries)
        entries, split_count, _ = split_long_subtitle_entries(entries)
        if translation_dir:
            stem = local_media_output_stem(original_name, "local_video")
            prepared_name = "split_en.srt" if split_count else "merged_en.srt"
            (translation_dir / prepared_name).write_text(
                entries_to_srt([{**e, "translation": ""} for e in entries], bilingual=False),
                encoding="utf-8",
            )

        api_url = opts.get("api_url", "").strip()
        api_key = opts.get("api_key", "").strip()
        model = opts.get("model", "").strip()
        if not api_url or not api_key or not model:
            raise RuntimeError("请填写 API 地址、API Key 和模型名后再翻译。")
        if opts.get("translation_reference") and translation_dir:
            write_translation_reference_file(
                translation_dir,
                opts.get("translation_reference", ""),
                opts.get("reference_materials", ""),
                original_name,
            )

        prompt_hint = build_translation_prompt_hint(
            opts.get("prompt", ""),
            opts.get("character_cn_names"),
        )
        local_task_update(task_id, progress=58, message="正在用大模型分析识别错误...")
        corrections = analyze_asr_corrections(
            entries, api_url, api_key, model, prompt_hint
        )
        applied = apply_asr_corrections(entries, corrections)

        translate_local_entries(task_id, entries, api_url, api_key, model, prompt_hint)
        if opts.get("compare_enabled"):
            cmp_api_url = opts.get("compare_api_url", "").strip()
            cmp_api_key = opts.get("compare_api_key", "").strip()
            cmp_model = opts.get("compare_model", "").strip()
            if not cmp_api_url or not cmp_api_key or not cmp_model:
                raise RuntimeError("已开启对比翻译，请填写对比翻译 API 地址、Key 和模型名。")
            translate_compare_entries(task_id, entries, cmp_api_url, cmp_api_key, cmp_model, prompt_hint)

        local_task_update(task_id, progress=94, message="正在导出字幕文件...")
        output_mode = opts.get("output_mode", "bilingual")
        bilingual = output_mode == "bilingual"
        order = opts.get("order", "zh_top")
        if output_mode == "en":
            output_text = entries_to_srt(
                [{**e, "translation": ""} for e in entries],
                bilingual=False,
                order=order,
            )
        elif output_mode == "zh":
            output_text = entries_to_srt(
                [{**e, "source": e.get("translation", ""), "translation": ""} for e in entries],
                bilingual=False,
                order=order,
            )
        else:
            order = "zh_top"
            output_text = entries_to_srt(entries, bilingual=bilingual, order=order)
        stem = local_media_output_stem(original_name, "local_video")
        output_name = subtitle_output_filename(stem, output_mode, order=order, context=opts)
        output_dir = translation_dir or DOWNLOADS_DIR
        output_path = output_dir / output_name
        output_path.write_text(output_text, encoding="utf-8")
        session = {
            "name": original_name,
            "entries": entries,
            "project_dir": str(project_dir) if project_dir else "",
            "translation_dir": str(translation_dir) if translation_dir else "",
            "source_path": opts.get("source_path") or media_path,
            "reference_materials": opts.get("reference_materials", ""),
            "translation_reference": opts.get("translation_reference", ""),
            "translation_model": model,
        }
        save_session_translation_artifacts(session, stem)
        session_id = create_local_session(
            original_name,
            entries,
            project_dir=project_dir,
            translation_dir=translation_dir,
            source_path=opts.get("source_path") or media_path,
            translation_model=model,
        )
        set_session_translation_reference(
            session_id,
            opts.get("translation_reference", ""),
            opts.get("reference_materials", ""),
        )

        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"完成：{len(entries)} 条字幕，合并 {merged_count} 处，智能分段 {split_count} 处，应用 {applied} 处校正",
            **file_download_payload(output_path),
            download_label="下载字幕",
            corrections=corrections,
            segments=len(entries),
            session_id=session_id,
        )
    except Exception as e:
        local_task_update(task_id, status="error", message=str(e), error=str(e))
    finally:
        if opts.get("cleanup_parent", True):
            try:
                shutil.rmtree(media_path.parent, ignore_errors=True)
            except Exception:
                pass


def run_local_import_task(task_id, media_path, original_name, opts):
    try:
        local_task_update(task_id, status="running", progress=2, message="正在通过在线 API 识别音视频...")
        srt_text = transcribe_media(task_id, media_path, opts)
        entries = parse_srt_content(srt_text)
        if not entries:
            raise RuntimeError("未识别出可用字幕。")
        project_dir = Path(opts["project_dir"]) if opts.get("project_dir") else None
        translation_dir = Path(opts["translation_dir"]) if opts.get("translation_dir") else None
        if translation_dir:
            translation_dir.mkdir(parents=True, exist_ok=True)
            (translation_dir / "raw_en.srt").write_text(srt_text, encoding="utf-8")
        if project_dir:
            _, subdirs = ensure_project_dirs(project_dir.name)
            media_stem = local_media_output_stem(original_name, "local_video")
            raw_sub_path = subdirs["subtitles"] / f"{media_stem}.raw_en.srt"
            raw_sub_path.write_text(srt_text, encoding="utf-8")
        session_id = create_local_session(
            original_name,
            entries,
            project_dir=project_dir,
            translation_dir=translation_dir,
            source_path=opts.get("source_path") or media_path,
        )
        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"完成：{len(entries)} 条字幕",
            session_id=session_id,
            segments=len(entries),
        )
    except Exception as e:
        local_task_update(task_id, status="error", message=str(e), error=str(e))
    finally:
        if opts.get("cleanup_parent", True):
            try:
                shutil.rmtree(media_path.parent, ignore_errors=True)
            except Exception:
                pass


def run_local_translate_task(task_id, session_id, opts):
    try:
        session = get_local_session(session_id)
        if not session:
            raise RuntimeError("字幕会话不存在，请重新导入。")
        entries = session["entries"]
        api_url = opts.get("api_url", "").strip()
        api_key = opts.get("api_key", "").strip()
        model = opts.get("model", "").strip()
        if not api_url or not api_key or not model:
            raise RuntimeError("请填写 API 地址、API Key 和模型名后再翻译。")
        local_task_update(task_id, status="running", progress=3, message="正在准备翻译...")
        set_session_translation_reference(
            session_id,
            opts.get("translation_reference", ""),
            opts.get("reference_materials", ""),
        )
        prompt_hint = build_translation_prompt_hint(
            opts.get("prompt", ""),
            opts.get("character_cn_names"),
        )
        translate_local_entries(task_id, entries, api_url, api_key, model, prompt_hint)
        if opts.get("compare_enabled"):
            cmp_api_url = opts.get("compare_api_url", "").strip()
            cmp_api_key = opts.get("compare_api_key", "").strip()
            cmp_model = opts.get("compare_model", "").strip()
            if not cmp_api_url or not cmp_api_key or not cmp_model:
                raise RuntimeError("已开启对比翻译，请填写对比翻译 API 地址、Key 和模型名。")
            translate_compare_entries(task_id, entries, cmp_api_url, cmp_api_key, cmp_model, prompt_hint)
        with local_subtitle_lock:
            if session_id in local_subtitle_sessions:
                local_subtitle_sessions[session_id]["entries"] = entries
                local_subtitle_sessions[session_id]["translation_model"] = model
                session = local_subtitle_sessions[session_id]
        save_session_translation_artifacts(session, local_media_output_stem(session.get("name"), "subtitle"))
        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"翻译完成：{len(entries)} 条字幕",
            session_id=session_id,
            segments=len(entries),
        )
    except Exception as e:
        local_task_update(task_id, status="error", message=str(e), error=str(e))


# ── Download Logic ────────────────────────────────────────────────────


REVIEW_ERROR_TYPES = [
    "新增信息",
    "漏译",
    "主语错误",
    "宾语错误",
    "逻辑错误",
    "否定错误",
    "程度错误",
    "术语错误",
    "中文不自然",
]
REVIEW_HARD_ERROR_TYPES = set(REVIEW_ERROR_TYPES) - {"中文不自然"}
REVIEW_CSV_FIELDS = [
    "编号", "时间轴", "英文原文", "A版中文", "B版中文", "最终采用", "最终中文",
    "A评分", "B评分", "A准确性", "A上下文一致性", "A中文自然度", "A字幕适配度", "A风险控制",
    "B准确性", "B上下文一致性", "B中文自然度", "B字幕适配度", "B风险控制",
    "风险等级", "错误类型", "原因",
]
REVIEW_SCORE_KEYS = [
    "accuracy",
    "context_consistency",
    "fluency",
    "subtitle_fit",
    "risk_control",
]
REVIEW_SCORE_MAX = {
    "accuracy": 40,
    "context_consistency": 20,
    "fluency": 20,
    "subtitle_fit": 10,
    "risk_control": 10,
}
REVIEW_PARSE_ERROR = "MODEL_JSON_PARSE_FAILED"


def review_summary_template():
    return {
        "total": 0,
        "unchanged": 0,
        "changed": 0,
        "use_a": 0,
        "use_b": 0,
        "use_z": 0,
        "tie": 0,
        "high_risk": 0,
        "model_parse_failed": 0,
        "a_better": 0,
        "b_better": 0,
        "z_generated": 0,
        "b_new_error_count": 0,
        "a_new_error_count": 0,
        "average_score_a": 0,
        "average_score_b": 0,
        "average_score_final": 0,
        "error_type_counts": {key: 0 for key in REVIEW_ERROR_TYPES},
    }


def split_bilingual_review_lines(lines):
    lines = [line.strip() for line in lines if line.strip()]
    if not lines:
        return "", ""
    if len(lines) == 1:
        return lines[0], ""

    cjk_positions = [i for i, line in enumerate(lines) if text_is_cjk(line)]
    if cjk_positions:
        first_cjk = cjk_positions[0]
        last_cjk = cjk_positions[-1]
        if first_cjk > 0:
            english = " ".join(lines[:first_cjk]).strip()
            chinese = "\n".join(lines[first_cjk:]).strip()
            return chinese, english
        if last_cjk < len(lines) - 1:
            chinese = "\n".join(lines[:last_cjk + 1]).strip()
            english = " ".join(lines[last_cjk + 1:]).strip()
            return chinese, english

    return "\n".join(lines[:-1]).strip(), lines[-1].strip()


def parse_bilingual_review_srt(content):
    entries = []
    content = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if content.startswith("WEBVTT"):
        content = re.sub(r"^WEBVTT[^\n]*(?:\n+)", "", content, count=1).strip()
    if not content:
        return entries

    blocks = re.split(r"\n\s*\n", content)
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_idx is None or time_idx >= len(lines) - 1:
            continue

        raw_index = None
        for line in lines[:time_idx]:
            if re.fullmatch(r"\d+", line):
                raw_index = int(line)
                break
        cn, en = split_bilingual_review_lines(lines[time_idx + 1:])
        entries.append({
            "index": raw_index or len(entries) + 1,
            "time": re.sub(r"\s+", " ", lines[time_idx]).strip(),
            "cn": cn,
            "en": en,
        })
    return entries


def normalize_review_english(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def normalize_review_space(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def review_risk_max(*levels):
    order = {"low": 0, "medium": 1, "high": 2}
    return max((level for level in levels if level in order), key=lambda x: order[x], default="low")


def align_review_entries(a_entries, b_entries):
    rows = []
    total = max(len(a_entries), len(b_entries))
    for pos in range(total):
        a = a_entries[pos] if pos < len(a_entries) else None
        b = b_entries[pos] if pos < len(b_entries) else None
        issues = []
        risk = "low"

        if not a or not b:
            issues.append("A/B 字幕条数不一致，当前行缺少一个版本。")
            risk = "high"

        index = (a or b or {}).get("index", pos + 1)
        if a and b and a.get("index") != b.get("index"):
            issues.append(f"编号不一致：A={a.get('index')}，B={b.get('index')}。")
            risk = "high"

        time_line = (a or b or {}).get("time", "")
        if a and b and a.get("time") != b.get("time"):
            issues.append(f"时间轴不一致：A={a.get('time')}，B={b.get('time')}。")
            risk = "high"

        en_a = (a or {}).get("en", "")
        en_b = (b or {}).get("en", "")
        english = en_a or en_b
        if a and b:
            if en_a == en_b:
                pass
            elif normalize_review_english(en_a) == normalize_review_english(en_b):
                issues.append("英文原文仅存在大小写、空格或标点差异。")
                risk = review_risk_max(risk, "medium")
            else:
                issues.append("英文原文明显不同，需要人工复核。")
                risk = "high"

        rows.append({
            "index": index,
            "time": time_line,
            "cn_a": (a or {}).get("cn", ""),
            "cn_b": (b or {}).get("cn", ""),
            "en": english,
            "en_a": en_a,
            "en_b": en_b,
            "risk": risk,
            "issues": issues,
            "missing": not a or not b,
        })
    return rows


def review_score_value(value, default=0, maximum=100):
    try:
        return max(0, min(maximum, int(round(float(value)))))
    except Exception:
        return default


def normalize_review_score_detail(detail):
    detail = detail if isinstance(detail, dict) else {}
    return {key: review_score_value(detail.get(key), 0, REVIEW_SCORE_MAX[key]) for key in REVIEW_SCORE_KEYS}


def normalize_review_score(score, detail):
    if score is not None:
        return review_score_value(score, 0, 100)
    return sum(review_score_value(detail.get(key), 0, 100) for key in REVIEW_SCORE_KEYS)


def normalize_review_errors(errors):
    normalized = []
    if not isinstance(errors, list):
        return normalized
    for item in errors:
        if isinstance(item, dict):
            err_type = str(item.get("type", "")).strip()
            detail = str(item.get("detail", "")).strip()
        else:
            err_type = str(item).strip()
            detail = ""
        if not err_type and not detail:
            continue
        normalized.append({"type": err_type or "其他", "detail": detail})
    return normalized


def review_has_hard_errors(errors):
    return any((err.get("type") or "").strip() in REVIEW_HARD_ERROR_TYPES for err in errors or [])


def review_context_item(row, x_is_a):
    return {
        "index": row.get("index"),
        "english": row.get("en", ""),
        "version_x": row.get("cn_a", "") if x_is_a else row.get("cn_b", ""),
        "version_y": row.get("cn_b", "") if x_is_a else row.get("cn_a", ""),
    }


def build_review_payload(rows, current_pos, before=2, after=2, x_is_a=True):
    start = max(0, current_pos - before)
    end = min(len(rows), current_pos + after + 1)
    current = rows[current_pos]
    payload = {
        "context_before": [review_context_item(rows[i], x_is_a) for i in range(start, current_pos)],
        "current": {
            "index": current.get("index"),
            "time": current.get("time", ""),
            "english": current.get("en", ""),
            "version_x": current.get("cn_a", "") if x_is_a else current.get("cn_b", ""),
            "version_y": current.get("cn_b", "") if x_is_a else current.get("cn_a", ""),
        },
        "context_after": [review_context_item(rows[i], x_is_a) for i in range(current_pos + 1, end)],
    }
    if current.get("issues"):
        payload["alignment_warnings"] = current.get("issues")
    return payload


def review_prompt_messages(payload, allow_z=True):
    system_prompt = """你是专业字幕翻译审校员。你会看到英文原文、上下文，以及两个中文译文版本。

你的任务不是重新翻译整段视频，而是判断当前字幕哪个译文更忠实、更适合作为中文字幕。

请严格遵守：

1. 英文原文是唯一事实依据。
2. 不要猜测哪个版本来自参考资料。
3. 不要因为某个版本更华丽就判更好。
4. 优先检查硬错误：新增信息、漏译、主语错误、宾语错误、否定错误、因果错误、程度错误、术语错误。
5. 只有在两个版本都没有硬错误时，才比较中文自然度和字幕可读性。
6. 如果两个版本只是风格差异，请判定为 TIE。
7. 如果两个版本都有问题，请生成一个 Z 版。
8. Z 版必须严格忠实英文，不允许添加英文没有的信息。
9. 输出必须是 JSON，不要输出解释性文字。

评分标准：
准确性 40 分：是否忠实英文原意，有无误译、漏译、增译。
上下文一致性 20 分：术语、主语、上下文衔接是否一致。
中文自然度 20 分：是否像自然中文字幕，是否拗口。
字幕适配度 10 分：是否简洁，是否适合屏幕阅读。
风险控制 10 分：是否避免幻觉、过度解释、擅自补充。

请输出 JSON：

{
  "choice": "X | Y | Z | TIE",
  "score_x": 0,
  "score_y": 0,
  "score_detail_x": {
    "accuracy": 0,
    "context_consistency": 0,
    "fluency": 0,
    "subtitle_fit": 0,
    "risk_control": 0
  },
  "score_detail_y": {
    "accuracy": 0,
    "context_consistency": 0,
    "fluency": 0,
    "subtitle_fit": 0,
    "risk_control": 0
  },
  "error_x": [
    {
      "type": "新增信息 | 漏译 | 主语错误 | 宾语错误 | 逻辑错误 | 否定错误 | 程度错误 | 术语错误 | 中文不自然",
      "detail": "具体说明"
    }
  ],
  "error_y": [],
  "better_reason": "简短说明为什么选择该版本",
  "final_cn": "最终采用的中文字幕。如果 choice 是 X，则填版本X文本；如果 choice 是 Y，则填版本Y文本；如果 choice 是 TIE，则填更适合作为最终字幕的文本；如果 choice 是 Z，则填新生成的严格忠实译文。",
  "risk_level": "low | medium | high"
}"""
    user_note = "下面是本次只审校 current 条目的上下文 JSON。不要重翻全片，只对 current 做对比、评分和选择。"
    if not allow_z:
        user_note += "\n本次配置不允许生成 Z 版。如果两个版本都有问题，请选择较少硬错误的一版，并把风险标为 high。"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_note + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def call_review_model(api_url, api_key, model, payload, allow_z=True):
    text = call_chat_model(api_url, api_key, model, review_prompt_messages(payload, allow_z=allow_z))
    data = extract_json_object(text)
    if not data:
        raise ValueError(REVIEW_PARSE_ERROR)
    return data


def normalize_review_choice(choice):
    choice = str(choice or "").upper().strip()
    if choice in {"X", "Y", "Z", "TIE"}:
        return choice
    for token in ("TIE", "X", "Y", "Z"):
        if token in choice:
            return token
    return ""


def build_review_result(row, x_is_a, model_data=None, tie_default="B", allow_z=True, parse_failed=False):
    model_data = model_data or {}
    detail_x = normalize_review_score_detail(model_data.get("score_detail_x"))
    detail_y = normalize_review_score_detail(model_data.get("score_detail_y"))
    score_x = normalize_review_score(model_data.get("score_x"), detail_x)
    score_y = normalize_review_score(model_data.get("score_y"), detail_y)
    errors_x = normalize_review_errors(model_data.get("error_x"))
    errors_y = normalize_review_errors(model_data.get("error_y"))
    choice = normalize_review_choice(model_data.get("choice"))
    reason = str(model_data.get("better_reason", "")).strip()
    risk_level = str(model_data.get("risk_level", "low")).lower().strip()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "low"
    risk_level = review_risk_max(risk_level, row.get("risk", "low"))

    if x_is_a:
        score_a, score_b = score_x, score_y
        detail_a, detail_b = detail_x, detail_y
        errors_a, errors_b = errors_x, errors_y
        x_version, y_version = "A", "B"
        x_text, y_text = row.get("cn_a", ""), row.get("cn_b", "")
    else:
        score_a, score_b = score_y, score_x
        detail_a, detail_b = detail_y, detail_x
        errors_a, errors_b = errors_y, errors_x
        x_version, y_version = "B", "A"
        x_text, y_text = row.get("cn_b", ""), row.get("cn_a", "")

    if parse_failed:
        choice = "PARSE_FAILED"
        final_choice = "B" if row.get("cn_b") else "A"
        final_cn = row.get("cn_b") or row.get("cn_a") or ""
        risk_level = "high"
        reason = "模型 JSON 解析失败，已按安全规则默认采用 B 版。" if row.get("cn_b") else "模型 JSON 解析失败，B 版缺失，采用 A 版。"
    elif choice == "X":
        final_choice = x_version
        final_cn = x_text
    elif choice == "Y":
        final_choice = y_version
        final_cn = y_text
    elif choice == "Z" and allow_z:
        final_choice = "Z"
        final_cn = str(model_data.get("final_cn", "")).strip() or row.get("cn_b") or row.get("cn_a", "")
    elif choice == "TIE":
        final_choice = "TIE_A" if tie_default == "A" else "TIE_B"
        final_cn = row.get("cn_a", "") if tie_default == "A" else row.get("cn_b", "")
        model_final = str(model_data.get("final_cn", "")).strip()
        if model_final and model_final != final_cn:
            reason = (reason + "；" if reason else "") + f"模型 TIE 文本：{model_final}"
    else:
        final_choice = "B" if row.get("cn_b") else "A"
        final_cn = row.get("cn_b") or row.get("cn_a") or ""
        risk_level = review_risk_max(risk_level, "medium")
        reason = (reason + "；" if reason else "") + "模型选择无效或 Z 被关闭，默认采用 B 版。"

    if final_choice in {"B", "TIE_B"} and review_has_hard_errors(errors_b) and not review_has_hard_errors(errors_a):
        final_choice = "A"
        final_cn = row.get("cn_a", "")
        risk_level = review_risk_max(risk_level, "high")
        reason = (reason + "；" if reason else "") + "B 版存在硬错误，按安全规则回退 A 版。"

    return {
        "choice": choice,
        "final_choice": final_choice,
        "final_cn": final_cn,
        "score_a": score_a,
        "score_b": score_b,
        "detail_a": detail_a,
        "detail_b": detail_b,
        "errors_a": errors_a,
        "errors_b": errors_b,
        "risk_level": risk_level,
        "reason": reason,
        "model_parse_failed": parse_failed,
    }


def unchanged_review_result(row):
    return {
        "choice": "UNCHANGED",
        "final_choice": "UNCHANGED",
        "final_cn": row.get("cn_a") or row.get("cn_b") or "",
        "score_a": "",
        "score_b": "",
        "detail_a": {key: "" for key in REVIEW_SCORE_KEYS},
        "detail_b": {key: "" for key in REVIEW_SCORE_KEYS},
        "errors_a": [],
        "errors_b": [],
        "risk_level": row.get("risk", "low"),
        "reason": "A/B 中文完全一致，未调用模型。" + ("；" + "；".join(row.get("issues", [])) if row.get("issues") else ""),
        "model_parse_failed": False,
    }


def missing_review_result(row, tie_default="B"):
    final_choice = "B" if tie_default == "B" and row.get("cn_b") else "A"
    if not row.get("cn_a"):
        final_choice = "B"
    if not row.get("cn_b"):
        final_choice = "A"
    return {
        "choice": "MISSING",
        "final_choice": final_choice,
        "final_cn": row.get("cn_b") if final_choice == "B" else row.get("cn_a", ""),
        "score_a": "",
        "score_b": "",
        "detail_a": {key: "" for key in REVIEW_SCORE_KEYS},
        "detail_b": {key: "" for key in REVIEW_SCORE_KEYS},
        "errors_a": [],
        "errors_b": [],
        "risk_level": "high",
        "reason": "A/B 当前行缺少一个版本，未调用模型；" + "；".join(row.get("issues", [])),
        "model_parse_failed": False,
    }


def review_error_types_text(result):
    types = []
    for err in result.get("errors_a", []) + result.get("errors_b", []):
        err_type = (err.get("type") or "").strip()
        if err_type and err_type not in types:
            types.append(err_type)
    if result.get("model_parse_failed"):
        types.append("model_parse_failed")
    return ";".join(types)


def review_reason_text(row, result):
    parts = []
    if result.get("reason"):
        parts.append(result["reason"])
    for issue in row.get("issues", []):
        if issue not in parts:
            parts.append(issue)
    details = []
    for label, errors in (("A", result.get("errors_a", [])), ("B", result.get("errors_b", []))):
        for err in errors:
            detail = (err.get("detail") or "").strip()
            err_type = (err.get("type") or "").strip()
            if detail:
                details.append(f"{label}:{err_type}:{detail}" if err_type else f"{label}:{detail}")
    parts.extend(details[:6])
    return "；".join(parts)


def update_review_summary(summary, result, changed):
    if changed:
        summary["changed"] += 1
    if result["final_choice"] == "UNCHANGED":
        summary["unchanged"] += 1
    if result["final_choice"] in {"A", "TIE_A"}:
        summary["use_a"] += 1
    elif result["final_choice"] in {"B", "TIE_B"}:
        summary["use_b"] += 1
    elif result["final_choice"] == "Z":
        summary["use_z"] += 1
        summary["z_generated"] += 1
    if result.get("choice") == "TIE":
        summary["tie"] += 1
    if result.get("risk_level") == "high":
        summary["high_risk"] += 1
    if result.get("model_parse_failed"):
        summary["model_parse_failed"] += 1
    if result.get("choice") in {"X", "Y"}:
        if result["final_choice"] == "A":
            summary["a_better"] += 1
        elif result["final_choice"] == "B":
            summary["b_better"] += 1
    summary["a_new_error_count"] += len(result.get("errors_a", []))
    summary["b_new_error_count"] += len(result.get("errors_b", []))
    for err in result.get("errors_a", []) + result.get("errors_b", []):
        err_type = (err.get("type") or "").strip()
        if err_type in summary["error_type_counts"]:
            summary["error_type_counts"][err_type] += 1


def write_cross_review_outputs(base_stem, rows, results, summary, output_detail=True):
    output_dir = DOWNLOADS_DIR / "review_outputs" / project_safe_name(f"{base_stem}_{time.strftime('%Y%m%d_%H%M%S')}")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = sanitize_filename(base_stem).strip().strip(".") or "subtitle"

    srt_path = output_dir / f"{safe_stem}.交叉审校合并版.srt"
    csv_path = output_dir / f"{safe_stem}.翻译评分报告.csv"
    json_path = output_dir / f"{safe_stem}.翻译评分汇总.json"
    zip_path = output_dir / f"{safe_stem}.交叉审校合并结果.zip"

    blocks = []
    for row, result in zip(rows, results):
        final_cn = (result.get("final_cn") or row.get("cn_b") or row.get("cn_a") or "").strip()
        english = (row.get("en") or row.get("en_a") or row.get("en_b") or "").strip()
        text_lines = [final_cn] if final_cn else []
        if english:
            text_lines.append(english)
        blocks.append(f"{row.get('index')}\n{row.get('time')}\n" + "\n".join(text_lines) + "\n")
    srt_path.write_text("\n".join(blocks), encoding="utf-8")

    if output_detail:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=REVIEW_CSV_FIELDS)
            writer.writeheader()
            for row, result in zip(rows, results):
                da = result.get("detail_a") or {}
                db = result.get("detail_b") or {}
                writer.writerow({
                    "编号": row.get("index"),
                    "时间轴": row.get("time", ""),
                    "英文原文": row.get("en", ""),
                    "A版中文": row.get("cn_a", ""),
                    "B版中文": row.get("cn_b", ""),
                    "最终采用": result.get("final_choice", ""),
                    "最终中文": result.get("final_cn", ""),
                    "A评分": result.get("score_a", ""),
                    "B评分": result.get("score_b", ""),
                    "A准确性": da.get("accuracy", ""),
                    "A上下文一致性": da.get("context_consistency", ""),
                    "A中文自然度": da.get("fluency", ""),
                    "A字幕适配度": da.get("subtitle_fit", ""),
                    "A风险控制": da.get("risk_control", ""),
                    "B准确性": db.get("accuracy", ""),
                    "B上下文一致性": db.get("context_consistency", ""),
                    "B中文自然度": db.get("fluency", ""),
                    "B字幕适配度": db.get("subtitle_fit", ""),
                    "B风险控制": db.get("risk_control", ""),
                    "风险等级": result.get("risk_level", "low"),
                    "错误类型": review_error_types_text(result),
                    "原因": review_reason_text(row, result),
                })
    else:
        csv_path.write_text("", encoding="utf-8-sig")

    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(srt_path, arcname=srt_path.name)
        if output_detail:
            zf.write(csv_path, arcname=csv_path.name)
        zf.write(json_path, arcname=json_path.name)

    return {
        "srt": path_download_info(srt_path),
        "csv": path_download_info(csv_path),
        "summary": path_download_info(json_path),
        "zip": path_download_info(zip_path),
    }


def run_cross_review_task(task_id, a_path, b_path, opts):
    try:
        local_task_update(task_id, status="running", progress=3, message="正在解析字幕...")
        a_text = Path(a_path).read_text(encoding="utf-8-sig", errors="replace")
        b_text = Path(b_path).read_text(encoding="utf-8-sig", errors="replace")
        a_entries = parse_bilingual_review_srt(a_text)
        b_entries = parse_bilingual_review_srt(b_text)
        if not a_entries or not b_entries:
            raise RuntimeError("没有解析到可用的双语 SRT，请确认每条包含中文译文和英文原文。")

        local_task_update(task_id, progress=10, message="正在检查对齐...")
        rows = align_review_entries(a_entries, b_entries)
        before = clamp_int(opts.get("context_before", 2), 0, 10, 2)
        after = clamp_int(opts.get("context_after", 2), 0, 10, 2)
        allow_z = bool(opts.get("allow_z", True))
        tie_default = "A" if str(opts.get("tie_default", "B")).upper() == "A" else "B"
        only_changed = bool(opts.get("only_changed", True))
        output_detail = bool(opts.get("output_detail", True))
        api_url = opts.get("api_url", "").strip()
        api_key = opts.get("api_key", "").strip()
        model = opts.get("model", "").strip()
        if not api_url or not api_key or not model:
            raise RuntimeError("请填写翻译/审校大模型 API 地址、Key 和模型名。")

        review_positions = [
            i for i, row in enumerate(rows)
            if not row.get("missing")
            and row.get("cn_a", "").strip() != row.get("cn_b", "").strip()
            and (
                not only_changed
                or normalize_review_space(row.get("cn_a")) != normalize_review_space(row.get("cn_b"))
            )
        ]
        total_reviews = len(review_positions)
        review_no = 0
        summary = review_summary_template()
        summary["total"] = len(rows)
        results = []
        score_a_values = []
        score_b_values = []
        score_final_values = []

        for pos, row in enumerate(rows):
            changed = normalize_review_space(row.get("cn_a")) != normalize_review_space(row.get("cn_b"))
            exact_same = row.get("cn_a", "").strip() == row.get("cn_b", "").strip()
            if row.get("missing"):
                result = missing_review_result(row, tie_default=tie_default)
            elif exact_same or (only_changed and not changed):
                result = unchanged_review_result(row)
            else:
                review_no += 1
                progress = 15 + round(review_no / max(1, total_reviews) * 70)
                local_task_update(
                    task_id,
                    status="running",
                    progress=progress,
                    message=f"正在审校第 {review_no} / {total_reviews} 条...",
                )
                x_is_a = random.choice([True, False])
                payload = build_review_payload(rows, pos, before=before, after=after, x_is_a=x_is_a)
                model_data = None
                parse_failed = False
                last_error = ""
                for _ in range(2):
                    try:
                        model_data = call_review_model(api_url, api_key, model, payload, allow_z=allow_z)
                        break
                    except ValueError as exc:
                        if str(exc) != REVIEW_PARSE_ERROR:
                            raise
                        last_error = "模型没有返回可解析的 JSON。"
                        model_data = None
                    except Exception as exc:
                        raise exc
                if model_data is None:
                    parse_failed = True
                    model_data = {"better_reason": last_error}
                result = build_review_result(
                    row,
                    x_is_a,
                    model_data=model_data,
                    tie_default=tie_default,
                    allow_z=allow_z,
                    parse_failed=parse_failed,
                )

            update_review_summary(summary, result, changed)
            if isinstance(result.get("score_a"), int):
                score_a_values.append(result["score_a"])
            if isinstance(result.get("score_b"), int):
                score_b_values.append(result["score_b"])
            if result.get("final_choice") in {"A", "TIE_A"} and isinstance(result.get("score_a"), int):
                score_final_values.append(result["score_a"])
            elif result.get("final_choice") in {"B", "TIE_B"} and isinstance(result.get("score_b"), int):
                score_final_values.append(result["score_b"])
            elif result.get("final_choice") == "Z":
                candidates = [v for v in (result.get("score_a"), result.get("score_b")) if isinstance(v, int)]
                if candidates:
                    score_final_values.append(max(candidates))
            results.append(result)

        def avg(values):
            return round(sum(values) / len(values), 2) if values else 0

        summary["average_score_a"] = avg(score_a_values)
        summary["average_score_b"] = avg(score_b_values)
        summary["average_score_final"] = avg(score_final_values)

        local_task_update(task_id, progress=88, message="正在生成最终 SRT...")
        base_stem = Path(opts.get("original_name") or Path(a_path).name).stem
        files = write_cross_review_outputs(base_stem, rows, results, summary, output_detail=output_detail)
        local_task_update(task_id, progress=96, message="正在生成评分报告...")
        time.sleep(0.1)
        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"完成：{len(rows)} 条字幕，审校 {total_reviews} 条，高风险 {summary['high_risk']} 条。",
            file=files["zip"]["file"],
            url=files["zip"]["url"],
            download_label="下载交叉审校结果ZIP",
            srt_file=files["srt"]["file"],
            srt_url=files["srt"]["url"],
            csv_file=files["csv"]["file"],
            csv_url=files["csv"]["url"],
            summary_file=files["summary"]["file"],
            summary_url=files["summary"]["url"],
            zip_file=files["zip"]["file"],
            zip_url=files["zip"]["url"],
            summary_data=summary,
        )
    except Exception as exc:
        local_task_update(task_id, status="error", message=str(exc), error=str(exc))


def fix_video_for_ae(downloads_dir, update=None):
    """Re-encode video to H.264+AAC MP4 for After Effects compatibility."""
    for f in list(downloads_dir.glob("*.mp4")):
        if ".hardsub" in f.name or ".ae." in f.name:
            continue
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v",
                 "-show-entries", "stream=codec_name", "-of", "csv=p=0", str(f)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            vcodec = probe.stdout.strip().split('\n')[0].strip() if probe.stdout.strip() else ''
            if vcodec in ('h264', 'avc', ''):
                continue
            if update:
                update("downloading", 95, f"Converting {vcodec}→H.264 for AE...")
            tmp = f.with_suffix('.ae.mp4')
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(f),
                 "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                 "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(tmp)],
                capture_output=True, timeout=1800,
            )
            if tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(f)
        except Exception:
            pass


def fix_audio_codec(downloads_dir, update=None):
    """Re-encode non-AAC audio in MP4 files for compatibility."""
    for f in list(downloads_dir.glob("*.mp4")):
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "a",
                 "-show-entries", "stream=codec_name", "-of", "csv=p=0", str(f)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            codec = probe.stdout.strip().split('\n')[0].strip() if probe.stdout.strip() else ''
            if codec and codec != 'aac':
                if update:
                    update("downloading", 95, f"Converting audio {codec}→aac...")
                tmp = f.with_suffix('.tmp.mp4')
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(f),
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-movflags", "+faststart", str(tmp)],
                    capture_output=True, timeout=600,
                )
                if tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(f)
        except Exception:
            pass


def get_format_string(codec, quality):
    base = {
        "best": "bestvideo",
        "h264": "bestvideo[vcodec^=avc]",
        "vp9": "bestvideo[vcodec^=vp9]",
        "av1": "bestvideo[vcodec^=av01]",
    }.get(codec, "bestvideo")
    h = f"[height<={quality}]" if quality != "best" else ""
    return (
        f"{base}{h}+bestaudio[acodec=aac]/"
        f"{base}{h}+bestaudio[acodec!=none]/"
        f"{base}{h}+bestaudio/"
        f"best{h}"
    )


def run_download(task_id, url, options):
    cmd = ["yt-dlp", "--no-warnings", "--ignore-errors", "--js-runtimes", "node", "--remote-components", "ejs:github"]
    download_type = options.get("type", "video")
    raw_items = options.get("download_items") or {}
    legacy_items = not isinstance(raw_items, dict) or not raw_items
    if not isinstance(raw_items, dict):
        raw_items = {}
    want_video = bool(raw_items.get("video", download_type == "video"))
    want_audio = bool(raw_items.get("audio", download_type == "audio"))
    if legacy_items and download_type == "video":
        want_audio = True
    want_cover = bool(raw_items.get("cover", True))
    want_description = bool(raw_items.get("description", True))
    want_translated_description = bool(raw_items.get("translated_description", False))
    want_link_title = bool(raw_items.get("link_title", True))
    description_options = options.get("description_options") or {}

    info = fetch_video_info(url)
    project_title = info.get("title") or options.get("project_title") or "untitled_video"
    project_dir, project_subdirs = ensure_project_dirs(project_title)
    need_timeline = want_description or (
        want_translated_description and bool(description_options.get("include_timeline", True))
    )
    timeline = extract_timeline_from_info(
        info,
        get_subtitle_timeline(url) if need_timeline else "",
    )
    write_project_notes(
        project_dir,
        project_subdirs,
        info,
        url,
        timeline,
        save_description=want_description,
        save_link_title=want_link_title,
    )
    output_template = str(project_dir / "%(title)s.%(ext)s")

    quality = options.get("quality", "1080")
    video_format = str(options.get("video_format", "mp4") or "mp4").lower()
    if video_format not in {"mp4"}:
        video_format = "mp4"
    audio_format = normalize_audio_format(options.get("audio_format", "mp3"))
    codec = options.get("codec", "best")
    dual_sub = bool(options.get("dual_subtitle", False) and want_video)
    sub_opts = options.get("sub_options", {})

    if want_audio and not want_video:
        cmd += ["-x", f"--audio-format={audio_format}", "--audio-quality=0"]
    elif want_video:
        format_id = options.get("format_id")
        if format_id:
            cmd += ["-f", format_id, "--merge-output-format", video_format]
        else:
            h = f"[height<={quality}]" if quality != "best" else ""
            cmd += ["-f", f"bv*{h}+ba/b{h}"]

            sort_parts = ["res", "fps", "hdr"]
            if codec == "h264":
                sort_parts.append("vcodec:h264")
            elif codec == "vp9":
                sort_parts.append("vcodec:vp9")
            elif codec == "av1":
                sort_parts.append("vcodec:av1")
            sort_parts += ["vbr", "abr"]
            cmd += ["-S", ",".join(sort_parts)]
            cmd += ["--merge-output-format", video_format]

        if dual_sub:
            sub_mode = options.get("sub_options", {}).get("sub_mode", "zh_en")
            sub_langs = "zh,zh-Hans,zh-Hant" if sub_mode == "zh" else "zh,zh-Hans,zh-Hant,en"
            cmd += [
                "--write-subs", "--write-auto-subs",
                "--sub-langs", sub_langs,
            ]
        elif options.get("subtitles", False):
            sub_langs = options.get("sub_langs", "zh,en")
            cmd += [
                "--write-subs", "--write-auto-subs",
                f"--sub-langs={sub_langs}", "--embed-subs",
            ]
    else:
        cmd += ["--skip-download"]

    if want_cover:
        cmd.append("--write-thumbnail")
    if want_description:
        cmd.append("--write-description")
    if want_link_title:
        cmd.append("--write-info-json")

    cmd += ["--newline", "--progress", "-o", output_template, url]

    def update(status, progress=None, message=None, files=None):
        with task_lock:
            tasks[task_id].update({
                "status": status,
                "progress": progress or tasks[task_id].get("progress", 0),
                "message": message or tasks[task_id].get("message", ""),
                "files": files or tasks[task_id].get("files", []),
                "project_dir": str(project_dir),
                "project": project_dir.name,
            })

    update("downloading", 0, f"Starting download into project: {project_dir.name}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(project_dir),
        )

        last_lines = []
        log_path = project_dir / "_download.log"
        logf = open(log_path, "w", encoding="utf-8")
        logf.write(f"CMD: {' '.join(cmd)}\n\n")
        logf.flush()

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            logf.write(line + "\n")
            logf.flush()
            last_lines.append(line)
            if len(last_lines) > 50:
                last_lines.pop(0)

            pct_match = re.search(r'(\d+\.?\d*)%', line)
            if pct_match:
                pct = float(pct_match.group(1))
                update("downloading", pct, line)

            if "[download]" in line.lower() and "destination" in line.lower():
                update("downloading", message=line)
            elif "merging" in line.lower():
                update("downloading", message="Merging audio and video...")
            elif "extracting" in line.lower():
                update("downloading", message="Extracting audio...")

        logf.close()
        process.wait()

        # Clean up .part files from interrupted downloads
        for f in project_dir.glob("*.part"):
            f.unlink(missing_ok=True)

        # yt-dlp may return non-zero if subtitle/metadata download fails,
        # but requested media might still have been written successfully.
        has_video = any(project_dir.glob("*.mp4"))
        audio_exts = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".opus", ".ogg"}
        has_audio = any(f.is_file() and f.suffix.lower() in audio_exts for f in project_dir.iterdir())

        if process.returncode == 0 or (want_video and has_video) or (want_audio and has_audio):
            if want_video and has_video:
                if options.get("ae_compat"):
                    fix_video_for_ae(project_dir, update)
                else:
                    fix_audio_codec(project_dir, update)

            if want_video and dual_sub:
                update("downloading", 95, "Processing dual subtitles...")
                ass_name = process_dual_subtitles(project_dir, sub_opts, update)
                if options.get("burn_sub") and ass_name:
                    update("downloading", 96, "Burning subtitles into video...")
                    burn_subtitles_to_video(project_dir, ass_name, update)
                elif ass_name:
                    update("downloading", 99, f"Subtitle file generated")
            if want_video and want_audio:
                ensure_audio_from_video(project_dir, project_subdirs, audio_format, update)
            standardize_project_mp3_files(project_dir, update)
            move_project_outputs(project_dir, project_subdirs)
            if want_cover:
                normalize_cover_images_to_jpeg(project_subdirs["source"], update)
            if want_translated_description:
                update("downloading", 98, "Generating translated Bilibili description...")
                try:
                    generate_and_save_ai_description(
                        project_dir,
                        project_subdirs,
                        info,
                        url,
                        description_options,
                        timeline=timeline,
                    )
                except Exception as desc_error:
                    update("error", message=f"翻译后简介生成失败: {desc_error}")
                    return
            update("completed", 100, f"Download complete: {project_dir.name}", list_downloaded_files())
        else:
            update("error", message=f"Download failed (exit code {process.returncode}): {'; '.join(last_lines[-5:])}")

    except Exception as e:
        update("error", message=str(e))


def list_downloaded_files():
    files = []
    ignored_parts = {"_local_tasks", "__pycache__"}
    for f in sorted(DOWNLOADS_DIR.rglob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if not f.is_file():
            continue
        rel = f.relative_to(DOWNLOADS_DIR)
        if any(part in ignored_parts or part.startswith(".") for part in rel.parts):
            continue
        if f.suffix not in ('.json', '.description'):
            size_mb = f.stat().st_size / (1024 * 1024)
            rel_name = str(rel).replace("\\", "/")
            files.append({
                "name": rel_name,
                "size": round(size_mb, 2),
                "ext": f.suffix.lstrip('.'),
                "url": f"/files/{urllib.parse.quote(rel_name, safe='/')}",
            })
    return files


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/local-subtitle/capabilities")
def local_subtitle_capabilities():
    return jsonify({
        "local_asr": False,
        "online_asr": {
            "volc_flash": {
                "name": "火山极速版",
                "resource_id": "volc.bigasr.auc_turbo",
                "local_file": True,
            },
            "volc_standard_url": {
                "name": "火山标准版 URL",
                "resource_id": "volc.bigasr.auc",
                "local_file": False,
            },
        },
    })


@app.route("/api/local-subtitle/import", methods=["POST"])
def import_local_subtitle():
    if "file" not in request.files:
        return jsonify({"error": "请先选择本地视频、音频或 SRT 文件"}), 400

    upload = request.files["file"]
    original_name = re.split(r"[\\/]", upload.filename or "local_file")[-1]
    safe_name = sanitize_filename(original_name) or f"{uuid.uuid4().hex}.tmp"
    suffix = Path(safe_name).suffix.lower()
    project_title = normalize_local_project_title(
        request.form.get("project_title", ""),
        original_name,
        "local_audio",
    )
    project_dir, project_subdirs = ensure_project_dirs(project_title)
    write_project_notes(project_dir, project_subdirs, {"title": project_title}, "", "")

    if suffix == ".srt":
        raw_bytes = upload.read()
        text = raw_bytes.decode("utf-8", errors="replace")
        entries = parse_srt_content(text)
        if not entries:
            return jsonify({"error": "没有解析到可用 SRT 字幕"}), 400
        source_srt = project_subdirs["subtitles"] / safe_name
        source_srt.write_bytes(raw_bytes)
        (project_subdirs["translation"] / "raw_en.srt").write_text(text, encoding="utf-8")
        session_id = create_local_session(
            original_name,
            entries,
            project_dir=project_dir,
            translation_dir=project_subdirs["translation"],
            source_path=source_srt,
        )
        return jsonify(local_session_payload(get_local_session(session_id)))

    task_id = str(uuid.uuid4())[:8]
    if suffix in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        media_dir = project_subdirs["video"]
    elif suffix in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".opus", ".ogg"):
        media_dir = project_subdirs["audio"]
    else:
        media_dir = project_subdirs["source"]
    media_path = media_dir / safe_name
    upload.save(media_path)

    opts = {
        "asr_provider": normalize_online_asr_provider(request.form.get("asr_provider")),
        "volc_api_key": request.form.get("volc_api_key", ""),
        "volc_resource_id": request.form.get("volc_resource_id", ""),
        "volc_audio_url": request.form.get("volc_audio_url", ""),
        "volc_segment_minutes": request.form.get("volc_segment_minutes", "25"),
        "language": request.form.get("language", "en"),
        "initial_prompt": request.form.get("initial_prompt", ""),
        "project_dir": str(project_dir),
        "translation_dir": str(project_subdirs["translation"]),
        "source_path": str(media_path),
        "cleanup_parent": False,
    }

    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入识别队列...",
            "session_id": None,
            "segments": 0,
        }

    threading.Thread(
        target=run_local_import_task,
        args=(task_id, media_path, original_name, opts),
        daemon=True,
    ).start()

    return jsonify({"task_id": task_id})


@app.route("/api/local-subtitle/session/<session_id>")
def get_local_subtitle_session(session_id):
    session = get_local_session(session_id)
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404
    return jsonify(local_session_payload(session))


@app.route("/api/local-subtitle/reference", methods=["POST"])
def build_local_translation_reference():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))

    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()
    if not api_url or not api_key or not model:
        return jsonify({"error": "请先填写 AI 设置里的 API 地址、Key 和模型"}), 400
    if not session and not (data.get("reference_materials", "").strip() or data.get("prompt", "").strip()):
        return jsonify({"error": "没有读取字幕时，请先填写视频标题、主题、人物、链接或关键词"}), 400

    try:
        reference = build_translation_reference(
            session["entries"] if session else [],
            api_url,
            api_key,
            model,
            materials=data.get("reference_materials", ""),
            user_hint=data.get("prompt", ""),
            entity_profile_boost=bool(data.get("entity_profile_boost", True)),
        )
        if session:
            output_path = set_session_translation_reference(
                session["id"],
                reference,
                data.get("reference_materials", ""),
            )
            payload = local_session_payload(get_local_session(session["id"]))
            if output_path:
                payload["reference_file"] = str(output_path.relative_to(DOWNLOADS_DIR)).replace("\\", "/")
        else:
            payload = {
                "translation_reference": reference,
                "reference_materials": data.get("reference_materials", ""),
                "research_only": True,
            }
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/local-path/pick", methods=["POST"])
def pick_local_path():
    data = request.json or {}
    kind = (data.get("kind") or "file").strip().lower()
    if kind not in {"file", "directory"}:
        return jsonify({"error": "路径选择类型无效"}), 400
    try:
        selected = pick_windows_local_path(kind)
        if not selected:
            return jsonify({"error": "未选择路径"}), 400
        return jsonify({"path": selected, "kind": kind})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open-download-directory", methods=["POST"])
def open_download_directory():
    data = request.json or {}
    target = None
    explicit_path = (data.get("path") or "").strip()
    if explicit_path:
        target = resolve_download_directory_candidate(explicit_path)
        if not target:
            return jsonify({"error": "只能打开 downloads 目录下已存在的文件夹"}), 400
    else:
        session = get_local_session(data.get("session_id", ""))
        if session:
            for key in ("translation_dir", "project_dir"):
                target = resolve_download_directory_candidate(session.get(key, ""))
                if target:
                    break
        if not target:
            target = DOWNLOADS_DIR.resolve()

    try:
        open_directory_with_system_manager(target)
        return jsonify({"ok": True, "path": str(target)})
    except Exception as e:
        return jsonify({"error": f"打开目录失败: {e}"}), 500


@app.route("/api/local-subtitle/clip-source", methods=["POST"])
def upload_clip_source():
    data = request.json or {}
    raw_path = (data.get("video_path") or "").strip().strip('"')
    if not raw_path:
        return jsonify({"error": "请填写或拖入本地视频/目录路径"}), 400
    local_path = parse_local_filesystem_path(raw_path)
    if not local_path.exists():
        return jsonify({"error": "路径不存在，请检查视频文件或目录路径是否完整"}), 400
    if local_path.is_dir():
        video_path = find_primary_video_in_directory(local_path)
        if not video_path:
            return jsonify({"error": "这个目录里没有找到可切片的视频文件"}), 400
    elif local_path.is_file():
        video_path = local_path
    else:
        return jsonify({"error": "路径不是可读取的视频文件或目录"}), 400
    if not is_video_file(video_path):
        return jsonify({"error": "切片路径只支持视频文件"}), 400

    original_name = video_path.name
    project_title = data.get("project_title", "").strip() or video_path.stem or "clip_source"
    clip_output_dir = default_original_clip_dir(video_path)
    session_id = create_local_session(
        original_name,
        [],
        project_dir=None,
        translation_dir=clip_output_dir,
        source_path=video_path,
    )
    with local_subtitle_lock:
        local_subtitle_sessions[session_id]["clip_source_mode"] = "path"
        local_subtitle_sessions[session_id]["clip_output_dir"] = str(clip_output_dir)
        local_subtitle_sessions[session_id]["project_title"] = project_title
    payload = local_session_payload(get_local_session(session_id))
    payload["clip_source"] = True
    payload["source_path"] = str(video_path)
    payload["clip_output_dir"] = str(clip_output_dir)
    return jsonify(payload)


@app.route("/api/local-subtitle/merge", methods=["POST"])
def merge_local_subtitle():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    entries, changed = merge_local_short_entries(
        session["entries"],
        max_chars_cjk=int(data.get("max_chars_cjk", 30)),
        max_words_latin=int(data.get("max_words_latin", 14)),
        max_gap=float(data.get("max_gap", 0.5)),
    )
    with local_subtitle_lock:
        local_subtitle_sessions[session["id"]]["entries"] = entries
    payload = local_session_payload(get_local_session(session["id"]))
    payload["merged"] = changed
    return jsonify(payload)


@app.route("/api/local-subtitle/split", methods=["POST"])
def split_local_subtitle():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    entries, changed, cleared = split_long_subtitle_entries(
        session["entries"],
        max_chars_latin=int(data.get("max_chars_latin", 84)),
        max_chars_cjk=int(data.get("max_chars_cjk", 42)),
    )
    with local_subtitle_lock:
        local_subtitle_sessions[session["id"]]["entries"] = entries
        local_subtitle_sessions[session["id"]]["corrections"] = []
        session = local_subtitle_sessions[session["id"]]

    translation_dir = session.get("translation_dir")
    if translation_dir:
        out_dir = Path(translation_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = local_media_output_stem(session.get("name"), "subtitle")
        source_entries = [{**e, "translation": ""} for e in entries]
        (out_dir / f"{stem}.split_en.srt").write_text(
            entries_to_srt(source_entries, bilingual=False),
            encoding="utf-8",
        )

    payload = local_session_payload(session)
    payload["split"] = changed
    payload["cleared_translations"] = cleared
    return jsonify(payload)


@app.route("/api/local-subtitle/analyze", methods=["POST"])
def analyze_local_subtitle():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()
    if not api_url or not api_key or not model:
        return jsonify({"error": "请先填写 AI 设置里的 API 地址、Key 和模型"}), 400

    corrections = analyze_asr_corrections(
        session["entries"], api_url, api_key, model, data.get("prompt", "")
    )
    with local_subtitle_lock:
        local_subtitle_sessions[session["id"]]["corrections"] = corrections
    return jsonify({"corrections": corrections, "count": len(corrections)})


@app.route("/api/local-subtitle/apply-corrections", methods=["POST"])
def apply_local_subtitle_corrections():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404
    corrections = data.get("corrections")
    if corrections is None:
        corrections = session.get("corrections", [])
    applied = apply_asr_corrections(session["entries"], corrections)
    with local_subtitle_lock:
        local_subtitle_sessions[session["id"]]["entries"] = session["entries"]
    save_session_translation_artifacts(session, local_media_output_stem(session.get("name"), "subtitle"))
    payload = local_session_payload(get_local_session(session["id"]))
    payload["applied"] = applied
    return jsonify(payload)


@app.route("/api/local-subtitle/clear-translations", methods=["POST"])
def clear_local_subtitle_translations():
    data = request.json or {}
    session_id = data.get("session_id", "")
    if not get_local_session(session_id):
        return jsonify({"error": "字幕会话不存在"}), 404

    cleared = 0
    cleared_compare = 0
    with local_subtitle_lock:
        session = local_subtitle_sessions.get(session_id)
        if not session:
            return jsonify({"error": "字幕会话不存在"}), 404
        for entry in session.get("entries", []):
            if (entry.get("translation") or "").strip():
                cleared += 1
            if (entry.get("translation_compare") or "").strip():
                cleared_compare += 1
            entry["translation"] = ""
            entry["translation_compare"] = ""
        session_snapshot = dict(session)

    save_session_translation_artifacts(session_snapshot, local_media_output_stem(session_snapshot.get("name"), "subtitle"))
    payload = local_session_payload(get_local_session(session_id))
    payload["cleared_translations"] = cleared
    payload["cleared_compare"] = cleared_compare
    return jsonify(payload)


@app.route("/api/local-subtitle/translate", methods=["POST"])
def translate_local_subtitle_session():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    task_id = str(uuid.uuid4())[:8]
    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入翻译队列...",
            "session_id": session["id"],
            "segments": len(session.get("entries", [])),
        }
    opts = {
        "api_url": data.get("api_url", ""),
        "api_key": data.get("api_key", ""),
        "model": data.get("model", ""),
        "prompt": data.get("prompt", ""),
        "reference_materials": data.get("reference_materials", ""),
        "translation_reference": data.get("translation_reference", ""),
        "character_cn_names": truthy_value(data.get("character_cn_names", False)),
        "compare_enabled": bool(data.get("compare_enabled", False)),
        "compare_api_url": data.get("compare_api_url", ""),
        "compare_api_key": data.get("compare_api_key", ""),
        "compare_model": data.get("compare_model", ""),
    }
    threading.Thread(
        target=run_local_translate_task,
        args=(task_id, session["id"], opts),
        daemon=True,
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/api/local-subtitle/export", methods=["POST"])
def export_local_subtitle():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    output_mode = data.get("output_mode", "bilingual")
    order = data.get("order", "zh_top")
    bilingual = output_mode == "bilingual"
    if output_mode == "en":
        export_entries = [{**e, "translation": ""} for e in session["entries"]]
        output_text = entries_to_srt(export_entries, bilingual=False, order=order)
    elif output_mode == "zh":
        export_entries = [{**e, "source": e.get("translation", ""), "translation": ""} for e in session["entries"]]
        output_text = entries_to_srt(export_entries, bilingual=False, order=order)
    else:
        order = "zh_top"
        output_text = entries_to_srt(session["entries"], bilingual=bilingual, order=order)
    if not output_text.strip():
        return jsonify({"error": "没有可导出的字幕内容"}), 400

    stem = local_media_output_stem(session.get("name"), "local_subtitle")
    output_name = subtitle_output_filename(stem, output_mode, order=order, context=session)
    output_dir = Path(session.get("translation_dir") or DOWNLOADS_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_name
    output_path.write_text(output_text, encoding="utf-8")
    payload = file_download_payload(output_path)
    payload["download_label"] = "下载字幕"
    return jsonify(payload)


@app.route("/api/burn-translated-video", methods=["POST"])
def burn_translated_video():
    data = request.json or {}
    task_id = str(uuid.uuid4())[:8]
    opts = {
        "session_id": data.get("session_id", ""),
        "font": data.get("font", "Microsoft YaHei"),
        "size": clamp_int(data.get("size", 52), 16, 200, 52),
        "color": data.get("color", "#FFFFFF"),
        "outline_color": data.get("outline_color", "#000000"),
        "sub_mode": data.get("sub_mode", "zh_en"),
        "sub_order": data.get("sub_order", "zh_top"),
        "sub_pos": clamp_int(data.get("sub_pos", 2), 1, 9, 2),
        "margin_v": clamp_int(data.get("margin_v", 30), 0, 1000, 30),
        "letter_spacing": clamp_int(data.get("letter_spacing", 0), -20, 100, 0),
        "line_spacing": clamp_int(data.get("line_spacing", 0), -80, 200, 0),
        "bg_enabled": bool(data.get("bg_enabled", True)),
        "bg_color": data.get("bg_color", "#000000"),
        "bg_opacity": clamp_int(data.get("bg_opacity", 50), 0, 100, 50),
        "bg_radius": clamp_int(data.get("bg_radius", 30), 0, 100, 30),
        "bg_width": clamp_int(data.get("bg_width", 80), 0, 100, 80),
        "bg_height": clamp_int(data.get("bg_height", 20), 0, 100, 20),
        "bg_offset_x": clamp_int(data.get("bg_offset_x", 0), -100, 100, 0),
        "bg_offset_y": clamp_int(data.get("bg_offset_y", 0), -100, 100, 0),
    }
    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入烧录队列...",
            "file": None,
            "url": None,
        }
    threading.Thread(
        target=run_burn_translated_video_task,
        args=(task_id, opts),
        daemon=True,
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/api/local-subtitle/clips", methods=["POST"])
def export_local_video_clips():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "请先读取上传的视频"}), 404
    clips = data.get("clips") or []
    if not isinstance(clips, list) or not clips:
        return jsonify({"error": "请至少添加一个切片时间段"}), 400

    task_id = str(uuid.uuid4())[:8]
    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入切片队列...",
            "session_id": session["id"],
            "clips": [],
        }
    threading.Thread(
        target=run_local_clip_task,
        args=(task_id, session["id"], clips),
        daemon=True,
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/api/test-chat-api", methods=["POST"])
def test_chat_api():
    data = request.json or {}
    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()
    if not api_url or not api_key or not model:
        return jsonify({"error": "请填写翻译 API 地址、Key 和模型名"}), 400
    try:
        reply = call_chat_model(
            api_url,
            api_key,
            model,
            [
                {"role": "system", "content": "Reply with exactly: OK"},
                {"role": "user", "content": "API test. Reply OK."},
            ],
        )
        return jsonify({
            "ok": True,
            "message": "翻译 API 可用",
            "reply": reply[:120],
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/local-subtitle/start", methods=["POST"])
def start_local_subtitle():
    if "file" not in request.files:
        return jsonify({"error": "请先选择本地视频或音频文件"}), 400

    upload = request.files["file"]
    original_name = re.split(r"[\\/]", upload.filename or "local_video.mp4")[-1]
    safe_name = sanitize_filename(original_name) or f"{uuid.uuid4().hex}.mp4"
    suffix = Path(safe_name).suffix.lower()
    project_title = normalize_local_project_title(
        request.form.get("project_title", ""),
        original_name,
        "local_video",
    )
    project_dir, project_subdirs = ensure_project_dirs(project_title)
    write_project_notes(project_dir, project_subdirs, {"title": project_title}, "", "")

    api_url = request.form.get("api_url", "").strip()
    api_key = request.form.get("api_key", "").strip()
    model = request.form.get("model", "").strip()
    if not api_url or not api_key or not model:
        return jsonify({"error": "请先填写 AI 设置里的 API 地址、Key 和模型"}), 400

    task_id = str(uuid.uuid4())[:8]
    if suffix in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        media_dir = project_subdirs["video"]
    elif suffix in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".opus", ".ogg"):
        media_dir = project_subdirs["audio"]
    else:
        media_dir = project_subdirs["source"]
    media_path = media_dir / safe_name
    upload.save(media_path)

    opts = {
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "prompt": request.form.get("prompt", ""),
        "reference_materials": request.form.get("reference_materials", ""),
        "translation_reference": request.form.get("translation_reference", ""),
        "character_cn_names": truthy_value(request.form.get("character_cn_names")),
        "compare_enabled": request.form.get("compare_enabled") == "1",
        "compare_api_url": request.form.get("compare_api_url", ""),
        "compare_api_key": request.form.get("compare_api_key", ""),
        "compare_model": request.form.get("compare_model", ""),
        "asr_provider": normalize_online_asr_provider(request.form.get("asr_provider")),
        "volc_api_key": request.form.get("volc_api_key", ""),
        "volc_resource_id": request.form.get("volc_resource_id", ""),
        "volc_audio_url": request.form.get("volc_audio_url", ""),
        "volc_segment_minutes": request.form.get("volc_segment_minutes", "25"),
        "language": request.form.get("language", "en"),
        "output_mode": request.form.get("output_mode", "bilingual"),
        "order": request.form.get("order", "zh_top"),
        "initial_prompt": request.form.get("initial_prompt", ""),
        "project_dir": str(project_dir),
        "translation_dir": str(project_subdirs["translation"]),
        "source_path": str(media_path),
        "cleanup_parent": False,
    }

    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入队列...",
            "file": None,
            "url": None,
            "segments": 0,
            "corrections": [],
        }

    thread = threading.Thread(
        target=run_local_subtitle_task,
        args=(task_id, media_path, original_name, opts),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/srt-review/start", methods=["POST"])
def start_srt_cross_review():
    if "file_a" not in request.files or "file_b" not in request.files:
        return jsonify({"error": "请选择 A 版和 B 版两个双语 SRT 文件。"}), 400

    api_url = request.form.get("api_url", "").strip()
    api_key = request.form.get("api_key", "").strip()
    model = request.form.get("model", "").strip() or "gpt-5.5"
    if not api_url or not api_key or not model:
        return jsonify({"error": "请填写翻译/审校大模型 API 地址、Key 和模型名。"}), 400

    def form_bool(name, default=False):
        raw = request.form.get(name)
        if raw is None:
            return default
        return str(raw).lower() in {"1", "true", "yes", "on"}

    task_id = str(uuid.uuid4())[:8]
    upload_dir = DOWNLOADS_DIR / "_review_uploads" / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    upload_a = request.files["file_a"]
    upload_b = request.files["file_b"]
    original_a = re.split(r"[\\/]", upload_a.filename or "version_a.srt")[-1]
    original_b = re.split(r"[\\/]", upload_b.filename or "version_b.srt")[-1]
    path_a = upload_dir / (sanitize_filename(original_a) or "version_a.srt")
    path_b = upload_dir / (sanitize_filename(original_b) or "version_b.srt")
    upload_a.save(path_a)
    upload_b.save(path_b)

    opts = {
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "context_before": request.form.get("context_before", 2),
        "context_after": request.form.get("context_after", 2),
        "allow_z": form_bool("allow_z", True),
        "tie_default": request.form.get("tie_default", "B"),
        "only_changed": form_bool("only_changed", True),
        "output_detail": form_bool("output_detail", True),
        "original_name": original_a,
    }

    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入交叉审校队列...",
            "file": None,
            "url": None,
        }

    threading.Thread(
        target=run_cross_review_task,
        args=(task_id, path_a, path_b, opts),
        daemon=True,
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/api/local-subtitle/status/<task_id>")
def local_subtitle_status(task_id):
    with local_subtitle_lock:
        task = dict(local_subtitle_tasks.get(task_id) or {})
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    yt_pattern = r'(youtube\.com|youtu\.be)'
    if not re.search(yt_pattern, url):
        return jsonify({"error": "Please provide a valid YouTube URL"}), 400

    download_items = data.get("download_items") or {}
    if download_items and isinstance(download_items, dict):
        allowed_items = ("video", "audio", "cover", "description", "translated_description", "link_title")
        download_items = {key: bool(download_items.get(key)) for key in allowed_items}
        if not any(download_items.values()):
            return jsonify({"error": "请至少选择一个下载内容"}), 400
    else:
        download_items = {}
    if download_items.get("translated_description") and not data.get("api_key", "").strip():
        return jsonify({"error": "生成翻译后简介需要填写下方翻译/简介 API Key"}), 400

    task_id = str(uuid.uuid4())[:8]
    with task_lock:
        tasks[task_id] = {
            "id": task_id,
            "url": url,
            "status": "queued",
            "progress": 0,
            "message": "Queued for download...",
            "files": [],
        }

    options = {
        "type": data.get("type", "video"),
        "download_items": download_items,
        "quality": data.get("quality", "1080"),
        "video_format": data.get("video_format", "mp4"),
        "audio_format": data.get("audio_format", "mp3"),
        "codec": data.get("codec", "best"),
        "subtitles": data.get("subtitles", False),
        "sub_langs": data.get("sub_langs", "zh,en"),
        "format_id": data.get("format_id"),
        "dual_subtitle": data.get("dual_subtitle", False),
        "sub_options": {
            "font": data.get("sub_font", "Microsoft YaHei"),
            "size": int(data.get("sub_size", 52)),
            "color": data.get("sub_color", "#FFFFFF"),
            "outline_color": data.get("outline_color", "#000000"),
            "sub_mode": data.get("sub_mode", "zh_en"),
            "sub_order": data.get("sub_order", "zh_top"),
            "sub_pos": int(data.get("sub_pos", 2)),
            "margin_v": int(data.get("margin_v", 30)),
            "letter_spacing": int(data.get("letter_spacing", 0)),
            "line_spacing": int(data.get("line_spacing", 0)),
            "bg_enabled": data.get("bg_enabled", False),
            "bg_color": data.get("bg_color", "#000000"),
            "bg_opacity": int(data.get("bg_opacity", 50)),
            "bg_radius": int(data.get("bg_radius", 30)),
            "bg_width": int(data.get("bg_width", 80)),
            "bg_height": int(data.get("bg_height", 20)),
            "bg_offset_x": int(data.get("bg_offset_x", 0)),
            "bg_offset_y": int(data.get("bg_offset_y", 0)),
            "translate_sub": data.get("translate_sub", False),
            "api_url": data.get("api_url", ""),
            "api_key": data.get("api_key", ""),
            "model": data.get("model", "gpt-4o-mini"),
        },
        "description_options": {
            "api_url": data.get("api_url", ""),
            "api_key": data.get("api_key", ""),
            "model": data.get("model", "gpt-4o-mini"),
            "style": data.get("description_style", ""),
            "include_timeline": data.get("description_include_timeline", True),
            "bilibili": data.get("description_bilibili", True),
        },
        "burn_sub": False,
        "ae_compat": data.get("ae_compat", False),
    }

    thread = threading.Thread(target=run_download, args=(task_id, url, options))
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def task_progress(task_id):
    def generate():
        while True:
            with task_lock:
                task = tasks.get(task_id)
                if not task:
                    yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                    break
                data = {
                    "status": task["status"],
                    "progress": task["progress"],
                    "message": task["message"],
                    "files": task["files"],
                }

            yield f"data: {json.dumps(data)}\n\n"

            if task["status"] in ("completed", "error"):
                break

            import time
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/files")
def list_files():
    return jsonify(list_downloaded_files())


@app.route("/api/formats", methods=["POST"])
def list_formats():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr[:300] or "Failed to fetch"}), 500

        info = json.loads(result.stdout)
        video_fmts, audio_fmts, combined = [], [], []

        for f in info.get("formats", []):
            has_v = f.get("vcodec", "none") != "none"
            has_a = f.get("acodec", "none") != "none"
            entry = {
                "id": f.get("format_id", ""),
                "ext": f.get("ext", ""),
                "res": f.get("resolution", ""),
                "width": f.get("width") or 0,
                "height": f.get("height") or 0,
                "fps": f.get("fps") or 0,
                "vcodec": f.get("vcodec", "none"),
                "acodec": f.get("acodec", "none"),
                "tbr": f.get("tbr") or 0,
                "vbr": f.get("vbr") or 0,
                "abr": f.get("abr") or 0,
                "filesize": f.get("filesize") or 0,
                "proto": f.get("protocol", ""),
            }
            if has_v and has_a:
                combined.append(entry)
            elif has_v:
                video_fmts.append(entry)
            elif has_a:
                audio_fmts.append(entry)

        video_fmts.sort(key=lambda x: (x["height"], x["vbr"]), reverse=True)
        audio_fmts.sort(key=lambda x: x["abr"], reverse=True)
        combined.sort(key=lambda x: (x["height"], x["tbr"]), reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            "video": video_fmts,
            "audio": audio_fmts,
            "combined": combined,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout while fetching formats"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/thumbnail", methods=["POST"])
def download_thumbnail():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr[:300] or "Failed to fetch info"}), 500

        info = json.loads(result.stdout)
        title = sanitize_filename(info.get("title", "thumbnail"))

        # Pick highest resolution thumbnail
        thumbs = info.get("thumbnails", [])
        best = None
        for t in thumbs:
            w = t.get("width") or 0
            if best is None or w > (best.get("width") or 0):
                best = t
        thumb_url = (best or {}).get("url", "") if best else ""

        if not thumb_url:
            return jsonify({"error": "No thumbnail found"}), 404

        import urllib.request
        raw_path = DOWNLOADS_DIR / f"{title}_thumb_raw"
        urllib.request.urlretrieve(thumb_url, str(raw_path))

        # Convert to JPEG with ffmpeg
        thumb_path = DOWNLOADS_DIR / f"{title}.jpeg"
        convert = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_path), "-frames:v", "1", "-q:v", "2", str(thumb_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        raw_path.unlink(missing_ok=True)
        if convert.returncode != 0 or not thumb_path.exists() or thumb_path.stat().st_size <= 0:
            thumb_path.unlink(missing_ok=True)
            return jsonify({"error": convert.stderr[-300:] or "Failed to convert thumbnail to JPEG"}), 500

        return jsonify({
            "saved": thumb_path.name,
            "title": info.get("title", ""),
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    filepath = (DOWNLOADS_DIR / filename).resolve()
    try:
        filepath.relative_to(DOWNLOADS_DIR.resolve())
    except Exception:
        return jsonify({"error": "Invalid file path"}), 400
    if filepath.exists() and filepath.is_file():
        base = filepath.stem
        for f in filepath.parent.iterdir():
            if f.stem == base or f.stem.startswith(base + "."):
                f.unlink(missing_ok=True)
        return jsonify({"ok": True})
    return jsonify({"error": "File not found"}), 404


@app.route("/api/clear", methods=["POST"])
def clear_files():
    removed = 0
    ignored = {"_local_tasks", "__pycache__"}
    for item in DOWNLOADS_DIR.iterdir():
        if item.name in ignored or item.name.startswith("."):
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
        elif item.is_file():
            item.unlink(missing_ok=True)
            removed += 1
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    PROJECTS_DIR.mkdir(exist_ok=True)
    with local_subtitle_lock:
        local_subtitle_sessions.clear()
        local_subtitle_tasks.clear()
    with task_lock:
        tasks.clear()
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/fetch-desc", methods=["POST"])
def fetch_description():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr[:300] or "Failed to fetch"}), 500

        info = json.loads(result.stdout)
        timeline = get_subtitle_timeline(url)
        return jsonify({
            "title": info.get("title", ""),
            "description": info.get("description", ""),
            "duration": info.get("duration", 0),
            "tags": info.get("tags", []),
            "uploader": info.get("uploader", ""),
            "channel": info.get("channel", ""),
            "timeline": timeline,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-desc", methods=["POST"])
def generate_description():
    data = request.json
    url = data.get("url", "").strip()
    style = data.get("style", "").strip()
    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "gpt-4o-mini").strip()
    include_timeline = data.get("include_timeline", False)
    bilibili = data.get("bilibili", False)

    if not url:
        return jsonify({"error": "目标视频 URL 不能为空"}), 400
    if not api_key:
        return jsonify({"error": "API Key 不能为空"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": "获取视频信息失败"}), 500

        info = json.loads(result.stdout)
        title = info.get("title", "")
        timeline = ""
        if include_timeline:
            timeline = get_subtitle_timeline(url)

        project_dir, project_subdirs = ensure_project_dirs(title or "untitled_video")
        write_project_notes(
            project_dir,
            project_subdirs,
            info,
            url,
            timeline,
            save_description=False,
            save_link_title=True,
        )
        save_path, generated = generate_and_save_ai_description(
            project_dir,
            project_subdirs,
            info,
            url,
            {
                "api_url": api_url,
                "api_key": api_key,
                "model": model,
                "style": style,
                "include_timeline": include_timeline,
                "bilibili": bilibili,
            },
            timeline=timeline,
        )

        return jsonify({
            "description": generated,
            "saved": str(save_path.relative_to(DOWNLOADS_DIR)).replace("\\", "/"),
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "获取视频信息超时"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


FONT_CN_NAMES = {
    "SmileySans": "得意黑",
    "SmileySans-Oblique": "得意黑 斜体",
    "SmileySans-Bold": "得意黑 粗体",
    "LXGWWenKai": "霞鹜文楷",
    "LXGWWenKai-Bold": "霞鹜文楷 粗体",
    "LXGWWenKai-Light": "霞鹜文楷 细体",
    "LXGWWenKaiMono": "霞鹜文楷等宽",
    "LXGWWenKaiScreen": "霞鹜文楷屏幕",
    "SourceHanSansSC": "思源黑体",
    "SourceHanSerifSC": "思源宋体",
    "SourceCodePro": "Source Code Pro",
    "FiraCode": "Fira Code",
    "JetBrainsMono": "JetBrains Mono",
    "NotoSansSC": "Noto Sans SC",
    "NotoSerifSC": "Noto Serif SC",
}


@app.route("/api/fonts")
def list_fonts():
    fonts = set()
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
        )
        i = 0
        while True:
            try:
                name, _value, _type = winreg.EnumValue(key, i)
                font_name = name.replace(" (TrueType)", "").replace(" (OpenType)", "").strip()
                if font_name:
                    fonts.add(font_name)
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)
    except Exception:
        pass

    user_font_dir = Path(os.path.expanduser("~/Fonts"))
    if user_font_dir.exists():
        for f in user_font_dir.rglob("*"):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                fonts.add(FONT_CN_NAMES.get(f.stem, f.stem))

    return jsonify({"fonts": sorted(fonts, key=str.lower)})


@app.route("/files/<path:filename>")
def serve_file(filename):
    return send_from_directory(str(DOWNLOADS_DIR), filename, as_attachment=True)


if __name__ == "__main__":
    import time
    import webbrowser
    from PySide6.QtWidgets import QApplication, QMainWindow
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    from PySide6.QtCore import QUrl

    port = int(os.environ.get("PORT", 5002))

    server = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    server.start()

    # Wait for Flask to be ready
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    try:
        qt_app = QApplication([])
        main_win = QMainWindow()
        main_win.setWindowTitle("YouTube Downloader")
        main_win.resize(1000, 750)

        profile = QWebEngineProfile("yt_dl", main_win)
        profile.setHttpUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = QWebEnginePage(profile, main_win)
        browser = QWebEngineView(main_win)
        browser.setPage(page)
        main_win.setCentralWidget(browser)
        main_win.show()
        browser.setUrl(QUrl(f"http://localhost:{port}"))
        qt_app.exec()
    except Exception:
        webbrowser.open(f"http://localhost:{port}")
        while True:
            time.sleep(1)
