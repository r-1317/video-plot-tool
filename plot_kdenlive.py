#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 r1317
"""番号付きの動画と台本を、既存の Kdenlive プロジェクトに配置する。"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction


VIDEO_EXTENSIONS = {".mov", ".mp4", ".mkv", ".avi", ".webm", ".m4v", ".mts"}


class ProjectError(Exception):
    """入力データやプロジェクトの構造に問題がある。"""


def prop(element: ET.Element, name: str, default: str = "") -> str:
    node = element.find(f"property[@name='{name}']")
    return node.text if node is not None and node.text is not None else default


def set_prop(element: ET.Element, name: str, value: object) -> None:
    node = element.find(f"property[@name='{name}']")
    if node is None:
        node = ET.Element("property", name=name)
        # MLT の property は entry / track / filter より先に置く。
        index = next((i for i, child in enumerate(element) if child.tag != "property"), len(element))
        element.insert(index, node)
    node.text = str(value)


def round_frame(value: Fraction) -> int:
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def frames(value: str, fps: Fraction) -> int:
    """MLT の整数フレーム、HH:MM:SS.mmm、HH:MM:SS:FF を読む。"""
    if re.fullmatch(r"\d+", value):
        return int(value)
    parts = value.replace(",", ".").split(":")
    if len(parts) not in (3, 4):
        raise ProjectError(f"対応していない時刻表記です: {value}")
    seconds = Fraction(parts[0]) * 3600 + Fraction(parts[1]) * 60 + Fraction(parts[2])
    return round_frame(seconds * fps) + (int(parts[3]) if len(parts) == 4 else 0)


def clock_time(count: int, fps: Fraction) -> str:
    milliseconds = round_frame(Fraction(count * 1000, 1) / fps)
    seconds, ms = divmod(milliseconds, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02}:{minute:02}:{sec:02}.{ms:03}"


def read_xml(path: Path, expected_root: str = "mlt") -> ET.ElementTree:
    try:
        tree = ET.parse(path)
    except ET.ParseError as error:
        raise ProjectError(f"XML を読み込めません: {path}: {error}") from error
    if tree.getroot().tag != expected_root:
        kind = ".kdenlivetitle タイトルクリップ" if expected_root == "kdenlivetitle" else "Kdenlive プロジェクト"
        raise ProjectError(f"{kind}ではありません: {path}")
    return tree


@dataclass
class Track:
    element: ET.Element
    playlists: list[ET.Element]
    index: int  # 黒背景トラックを除いた、sequence 内の 0 始まりの番号。
    audio: bool
    label: str = ""

    @property
    def name(self) -> str:
        return prop(self.element, "kdenlive:track_name")

    def require_empty(self) -> None:
        if any(p.find("entry") is not None for p in self.playlists) or self.element.find("transition") is not None:
            raise ProjectError(f"配置先 {self.label} ({self.name or self.element.get('id')}) は空ではありません。")
        if prop(self.element, "kdenlive:locked") == "1":
            raise ProjectError(f"配置先 {self.label} はロックされています。")


class Project:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.tree = read_xml(path)
        self.root = self.tree.getroot()
        self.ids = {el.get("id"): el for el in self.root if el.get("id")}
        self.bin = self.ids.get("main_bin")
        profile = self.root.find("profile")
        if self.bin is None or profile is None:
            raise ProjectError("main_bin または profile がありません。")
        self.profile = profile
        self.fps = Fraction(int(profile.get("frame_rate_num", "0")), int(profile.get("frame_rate_den", "1")))
        if self.fps <= 0:
            raise ProjectError("プロジェクトのフレームレートが不正です。")
        active = prop(self.bin, "kdenlive:docproperties.activetimeline")
        sequences = [el for el in self.root.findall("tractor") if prop(el, "kdenlive:uuid")]
        candidates = [el for el in sequences if prop(el, "kdenlive:uuid") == active] if active else sequences
        if len(candidates) != 1:
            raise ProjectError("編集対象のシーケンスを特定できません。Kdenlive で対象を開いて保存してください。")
        self.sequence = candidates[0]
        self.tracks: list[Track] = []
        for ref in self.sequence.findall("track"):
            el = self.ids.get(ref.get("producer"))
            if el is None:
                raise ProjectError("シーケンスに参照切れがあります。")
            if prop(el, "kdenlive:playlistid") == "black_track":
                continue
            if el.tag != "tractor":
                raise ProjectError("対応していないトラック構造です。")
            playlists = [self.ids.get(t.get("producer")) for t in el.findall("track")]
            if len(playlists) != 2 or any(p is None or p.tag != "playlist" for p in playlists):
                raise ProjectError(f"トラック {el.get('id')} の playlist 構造に対応していません。")
            self.tracks.append(Track(el, playlists, len(self.tracks), prop(el, "kdenlive:audio_track") == "1"))
        audio_count = sum(t.audio for t in self.tracks)
        video_count = 0
        for track in self.tracks:
            if track.audio:
                track.label = f"A{audio_count}"
                audio_count -= 1
            else:
                video_count += 1
                track.label = f"V{video_count}"
        self.used_ids = {el.get("id") for el in self.root.iter() if el.get("id")}
        bin_ids = [int(p.text) for p in self.root.iter("property")
                   if p.get("name") == "kdenlive:id" and (p.text or "").isdigit()]
        self.next_bin_id = max(bin_ids, default=0) + 1

    def select_track(self, selector: str, audio: bool) -> Track:
        exact = [t for t in self.tracks if t.element.get("id") == selector or t.label == selector]
        candidates = exact or [t for t in self.tracks if t.name == selector]
        if len(candidates) != 1 or candidates[0].audio != audio:
            raise ProjectError(f"トラック {selector!r} を特定できません。--list-tracks で確認してください。")
        return candidates[0]

    def xml_id(self, prefix: str) -> str:
        n = 0
        while f"{prefix}{n}" in self.used_ids:
            n += 1
        result = f"{prefix}{n}"
        self.used_ids.add(result)
        return result

    def bin_id(self) -> str:
        result = str(self.next_bin_id)
        self.next_bin_id += 1
        return result


def read_inputs(directory: Path, script: Path, encoding: str) -> list[tuple[int, Path, str]]:
    if not directory.is_dir():
        raise ProjectError(f"動画フォルダーがありません: {directory}")
    lines = script.read_text(encoding=encoding).splitlines()
    if not lines:
        raise ProjectError("台本が空です。")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise ProjectError(f"台本の {number} 行目が空です。行番号の対応を確認してください。")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", line):
            raise ProjectError(f"台本の {number} 行目に XML で使えない制御文字があります。")
    videos: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        match = re.match(r"^(\d+)(?:\D|$)", unicodedata.normalize("NFKC", path.stem))
        if not match:
            continue  # 番号のない読み上げ等は対象外。
        number = int(match[1])
        if number in videos:
            raise ProjectError(f"動画の番号 {number:03} が重複しています: {videos[number].name}, {path.name}")
        videos[number] = path.resolve()
    expected = set(range(1, len(lines) + 1))
    missing_videos = expected - videos.keys()
    missing_lines = videos.keys() - expected
    problems = []
    if missing_videos:
        problems.append("動画がない行: " + ", ".join(f"{n:03}" for n in sorted(missing_videos)))
    if missing_lines:
        problems.append("対応する台本の行がない動画: " + ", ".join(f"{n:03}" for n in sorted(missing_lines)))
    if problems:
        raise ProjectError(" / ".join(problems))
    return [(n, videos[n], line) for n, line in enumerate(lines, 1)]


@dataclass
class Media:
    path: Path
    length: int
    video: dict
    audio: dict


def probe_media(path: Path, fps: Fraction, ffprobe: str) -> Media:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    if result.returncode:
        raise ProjectError(f"動画を解析できません: {path.name}\n{result.stderr.strip()}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise ProjectError(f"映像と音声の両方が必要です: {path.name}")
    # コンテナーの尺には AAC のパディングが含まれ得るため、映像の尺を優先。
    duration = None
    try:
        timestamp_duration = Fraction(str(video.get("duration_ts"))) * Fraction(str(video.get("time_base")))
    except (ValueError, ZeroDivisionError):
        timestamp_duration = None
    for value in (timestamp_duration, video.get("duration"), info.get("format", {}).get("duration")):
        try:
            candidate = Fraction(str(value))
            if candidate > 0:
                duration = candidate
                break
        except (ValueError, ZeroDivisionError):
            continue
    if duration is None:
        raise ProjectError(f"動画の長さを取得できません: {path.name}")
    length = round_frame(duration * fps)
    if length < 1:
        raise ProjectError(f"動画が 1 フレームより短いです: {path.name}")
    return Media(path, length, video, audio)


def validate_title_template(project: Project, template: ET.Element) -> ET.Element:
    if template.tag != "kdenlivetitle":
        raise ProjectError("字幕見本は kdenlivetitle 形式である必要があります。")
    if any(template.get(key) != project.profile.get(key) for key in ("width", "height")):
        raise ProjectError("字幕見本と入力プロジェクトの解像度が異なります。")
    contents = template.findall("item[@type='QGraphicsTextItem']/content")
    if len(contents) != 1 or len(template.findall("item")) != 1:
        raise ProjectError("字幕見本にはテキスト要素が 1 個だけ必要です。")
    if list(contents[0]) or any("richtext" in key for key in contents[0].attrib):
        raise ProjectError("リッチテキストの字幕見本には対応していません。プレーンテキストのタイトルを使ってください。")
    return template


def title_template(project: Project, style_path: Path | None,
                   style_title_path: Path | None = None) -> ET.Element:
    if style_path is not None and style_title_path is not None:
        raise ProjectError("--style-project と --style-title は同時に指定できません。")
    if style_title_path is not None:
        return validate_title_template(project, read_xml(style_title_path, expected_root="kdenlivetitle").getroot())
    source = read_xml(style_path).getroot() if style_path else project.root
    candidates = []
    for producer in source.findall("producer"):
        name = unicodedata.normalize("NFKC", prop(producer, "kdenlive:clipname"))
        if prop(producer, "mlt_service") == "kdenlivetitle" and re.fullmatch(r"字幕\d+", name):
            candidates.append((int(name[2:]), producer))
    if candidates:
        template = ET.fromstring(prop(min(candidates, key=lambda item: item[0])[1], "xmldata"))
        return validate_title_template(project, template)
    if style_path:
        raise ProjectError("見本プロジェクトに『字幕001』などの番号付きタイトルがありません。")
    width, height = int(project.profile.get("width")), int(project.profile.get("height"))
    template = ET.Element("kdenlivetitle", width=str(width), height=str(height), LC_NUMERIC="C")
    item = ET.SubElement(template, "item", {"type": "QGraphicsTextItem", "z-index": "0"})
    position = ET.SubElement(item, "position", x=str(round(width * 0.025)), y=str(round(height * 0.83)))
    ET.SubElement(position, "transform").text = "1,0,0,0,1,0,0,0,1"
    ET.SubElement(item, "content", {
        "alignment": "1", "font": "sans-serif", "font-pixel-size": str(round(height * 0.052)),
        "font-color": "255,255,255,255", "font-weight": "400", "font-italic": "0",
        "font-underline": "0", "font-outline": str(max(1, round(height * 0.005))),
        "font-outline-color": "0,0,0,255", "letter-spacing": "0", "line-spacing": "0",
        "typewriter": "0;2;1;0;0",
    })
    for tag in ("startviewport", "endviewport"):
        ET.SubElement(template, tag, rect=f"0,0,{width},{height}")
    ET.SubElement(template, "background", color="0,0,0,0")
    return template


def make_media(project: Project, media: Media, bin_id: str) -> list[ET.Element]:
    master = ET.Element("chain", id=project.xml_id("chain"), **{"in": "0", "out": str(media.length - 1)})
    values = {
        "length": media.length, "eof": "pause", "resource": media.path,
        "mlt_service": "avformat", "seekable": 1,
        "audio_index": media.audio["index"], "video_index": media.video["index"],
        "astream": 0, "vstream": 0, "mute_on_pause": 0,
        "kdenlive:id": bin_id, "kdenlive:folderid": -1, "kdenlive:clip_type": 0,
        "kdenlive:clipname": media.path.name,
        "kdenlive:control_uuid": "{" + str(uuid.uuid4()) + "}",
        "kdenlive:file_size": media.path.stat().st_size,
        "meta.media.width": media.video["width"], "meta.media.height": media.video["height"],
    }
    for key, value in values.items():
        set_prop(master, key, value)
    elements = [master]
    for audio_only in (False, True):
        clone = copy.deepcopy(master)
        clone.set("id", project.xml_id("chain"))
        set_prop(clone, "set.test_audio", int(not audio_only))
        set_prop(clone, "set.test_image", int(audio_only))
        elements.append(clone)
    return elements  # bin, video, audio


def make_title(project: Project, template: ET.Element, text: str, number: int, length: int, bin_id: str) -> ET.Element:
    title = copy.deepcopy(template)
    title.set("duration", str(length))
    title.set("out", str(length - 1))
    content = title.find("item[@type='QGraphicsTextItem']/content")
    content.text = text
    # 古い文の幅・高さや固定幅を再利用すると、切れや自動改行の原因になる。
    # これらを省略すると MLT / Qt が今回の文字列の自然な寸法を計算する。
    for attr in ("box-width", "box-height", "textwidth", "preferred-width"):
        content.attrib.pop(attr, None)
    producer = ET.Element("producer", id=project.xml_id("producer"), **{"in": "0", "out": str(length - 1)})
    for key, value in {
        "length": length, "eof": "pause", "resource": "", "mlt_service": "kdenlivetitle",
        "meta.media.progressive": 1, "aspect_ratio": 1, "seekable": 1,
        "kdenlive:duration": clock_time(length, project.fps), "kdenlive:clipname": f"字幕{number:03}",
        "xmldata": ET.tostring(title, encoding="unicode"), "kdenlive:folderid": -1,
        "kdenlive:id": bin_id, "kdenlive:clip_type": 2,
        "kdenlive:control_uuid": "{" + str(uuid.uuid4()) + "}",
        "kdenlive:uniqueId": "{" + str(uuid.uuid4()) + "}", "force_reload": 0,
        "meta.media.width": project.profile.get("width"), "meta.media.height": project.profile.get("height"),
    }.items():
        set_prop(producer, key, value)
    return producer


def add_entry(playlist: ET.Element, producer: ET.Element, length: int, timeline: bool = True) -> None:
    entry = ET.SubElement(playlist, "entry", {"producer": producer.get("id"), "in": "0", "out": str(length - 1)})
    if timeline:
        set_prop(entry, "kdenlive:id", prop(producer, "kdenlive:id"))


def arrange(project: Project, items: list[tuple[int, Media, str]], template: ET.Element,
            video_track: Track, audio_track: Track, subtitle_track: Track) -> int:
    targets = (video_track, audio_track, subtitle_track)
    if len({t.index for t in targets}) != 3:
        raise ProjectError("映像・音声・字幕はそれぞれ別のトラックを指定してください。")
    for track in targets:
        track.require_empty()
    groups_key = "kdenlive:sequenceproperties.groups"
    groups = json.loads(prop(project.sequence, groups_key, "[]"))
    if not isinstance(groups, list):
        raise ProjectError("シーケンスのグループ情報が不正です。")
    for track in targets:
        for playlist in track.playlists:
            for blank in list(playlist.findall("blank")):
                playlist.remove(blank)
            for attr in ("in", "out"):
                playlist.attrib.pop(attr, None)
    generated = []
    cursor = 0
    for number, media, text in items:
        master, video, audio = make_media(project, media, project.bin_id())
        title = make_title(project, template, text, number, media.length, project.bin_id())
        generated.extend((master, video, audio, title))
        add_entry(project.bin, master, media.length, timeline=False)
        add_entry(project.bin, title, media.length, timeline=False)
        for track, producer in zip(targets, (video, audio, title)):
            add_entry(track.playlists[0], producer, media.length)
        groups.append({"type": "AVSplit", "children": [
            {"type": "Leaf", "leaf": "clip", "data": f"{track.index}:{cursor}:-1"}
            for track in (video_track, audio_track)
        ]})
        cursor += media.length
    # MLT は参照先の定義が先に必要。新規 producer は全 playlist より前に置く。
    insertion = next(i for i, el in enumerate(project.root) if el.tag in ("playlist", "tractor"))
    project.root[insertion:insertion] = generated
    for track in targets:
        track.element.set("in", "0")
        track.element.set("out", str(cursor - 1))
        for ref in project.sequence.findall("track"):
            if ref.get("producer") == track.element.get("id"):
                ref.attrib.pop("in", None)
                ref.attrib.pop("out", None)
    set_prop(project.sequence, groups_key, json.dumps(groups, ensure_ascii=False, indent=2))
    # 背景や BGM 等の既存タイムラインは縮めず、必要な場合だけ全体の尺を延ばす。
    old_length = frames(project.sequence.get("out", "0"), project.fps) + 1
    total = max(cursor, old_length)
    project.sequence.set("out", str(total - 1))
    set_prop(project.sequence, "kdenlive:duration", clock_time(total, project.fps))
    set_prop(project.sequence, "kdenlive:maxduration", total)
    for entry in project.bin.findall("entry"):
        if entry.get("producer") == project.sequence.get("id"):
            entry.set("out", str(total - 1))
    for wrapper in project.root.findall("tractor"):
        if prop(wrapper, "kdenlive:projectTractor") != "1":
            continue
        for ref in wrapper.findall("track"):
            if ref.get("producer") == project.sequence.get("id"):
                ref.set("out", str(total - 1))
                wrapper.set("out", str(total - 1))
    for ref in project.sequence.findall("track"):
        producer = project.ids.get(ref.get("producer"))
        if producer is not None and prop(producer, "kdenlive:playlistid") == "black_track":
            if frames(producer.get("out", "0"), project.fps) < total - 1:
                producer.set("out", str(total - 1))
    # 出力先が別フォルダーでも既存の相対リソースを同じ場所から解決する。
    root_path = Path(project.root.get("root") or ".")
    if not root_path.is_absolute():
        root_path = project.path.parent / root_path
    project.root.set("root", str(root_path.resolve()))
    return cursor


def write_project(tree: ET.ElementTree, output: Path, force: bool) -> None:
    """同一ディレクトリの一時ファイルを経由し、不完全な出力を残さない。"""
    ET.indent(tree, space=" ")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".arrange-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            tree.write(stream, encoding="utf-8", xml_declaration=True)
        if force:
            os.replace(temporary, output)
        else:
            # 出力の存在確認後に他プロセスが作ったファイルも上書きしない。
            os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("project", type=Path, help="配置先の空トラックを持つ .kdenlive ファイル")
    result.add_argument("--videos", type=Path, help="001_...mov などの番号付き動画のフォルダー")
    result.add_argument("--script", type=Path, help="1 行目が 001 に対応する台本")
    style = result.add_mutually_exclusive_group()
    style.add_argument("--style-project", type=Path, help="字幕001 の装飾を取得する見本プロジェクト")
    style.add_argument("--style-title", type=Path, help="字幕の装飾を取得する .kdenlivetitle タイトルクリップ")
    result.add_argument("--output", "-o", type=Path, help="出力先（既定: 入力名-arranged.kdenlive）")
    result.add_argument("--video-track", default="立ち絵", help="映像トラックの名前・V番号・tractor ID（既定: 立ち絵）")
    result.add_argument("--audio-track", default="A2", help="音声トラックの名前・A番号・tractor ID（既定: A2）")
    result.add_argument("--subtitle-track", default="字幕", help="字幕トラックの名前・V番号・tractor ID（既定: 字幕）")
    result.add_argument("--encoding", default="utf-8-sig", help="台本の文字コード（既定: UTF-8、BOM 可）")
    result.add_argument("--ffprobe", default="ffprobe", help="ffprobe の実行ファイル")
    result.add_argument("--list-tracks", action="store_true", help="トラックを一覧表示して終了")
    result.add_argument("--dry-run", action="store_true", help="検証と配置予定の表示のみ。ファイルは作らない")
    result.add_argument("--force", action="store_true", help="既存の出力ファイルを置き換える（入力ファイルは不可）")
    return result


def run(args: argparse.Namespace) -> None:
    project = Project(args.project)
    if args.list_tracks:
        for track in reversed(project.tracks):
            count = sum(len(p.findall("entry")) for p in track.playlists)
            print(f"{track.label:>3}  {track.element.get('id'):12}  {track.name or '(名前なし)':12}  クリップ {count}")
        return
    if args.videos is None or args.script is None:
        raise ProjectError("--videos と --script を指定してください。")
    output = (args.output or args.project.with_name(args.project.stem + "-arranged.kdenlive")).resolve()
    protected = [path for path in (args.project, args.script, args.style_project, args.style_title) if path is not None]
    if any(output == path.resolve() or (output.exists() and output.samefile(path)) for path in protected):
        raise ProjectError("出力先には入力ファイルと異なるパスを指定してください。")
    if output.suffix.lower() != ".kdenlive":
        raise ProjectError("出力ファイルの拡張子は .kdenlive にしてください。")
    if output.exists() and not args.force and not args.dry_run:
        raise ProjectError(f"出力先が既にあります: {output}（置き換える場合は --force）")
    if not output.parent.is_dir():
        raise ProjectError(f"出力先フォルダーがありません: {output.parent}")
    video_track = project.select_track(args.video_track, audio=False)
    audio_track = project.select_track(args.audio_track, audio=True)
    subtitle_track = project.select_track(args.subtitle_track, audio=False)
    for track in (video_track, audio_track, subtitle_track):
        track.require_empty()
    inputs = read_inputs(args.videos, args.script, args.encoding)
    template = title_template(project, args.style_project, args.style_title)
    ffprobe = shutil.which(args.ffprobe)
    if ffprobe is None:
        raise ProjectError("ffprobe が見つかりません。FFmpeg をインストールするか --ffprobe で指定してください。")
    items = []
    cursor = 0
    print(f"FPS: {project.fps} / 映像: {video_track.label} / 音声: {audio_track.label} / 字幕: {subtitle_track.label}")
    for number, path, text in inputs:
        media = probe_media(path, project.fps, ffprobe)
        items.append((number, media, text))
        print(f"{number:03}  {clock_time(cursor, project.fps)}  {media.length:5} frames  {path.name}", flush=True)
        cursor += media.length
    total = arrange(project, items, template, video_track, audio_track, subtitle_track)
    print(f"合計: {len(items)} 本 / {total} frames / {clock_time(total, project.fps)}")
    if args.dry_run:
        print("検証完了（ファイルは作成していません）。")
    else:
        write_project(project.tree, output, args.force)
        print(f"作成しました: {output}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        run(args)
    except (ProjectError, OSError, ValueError, LookupError, subprocess.TimeoutExpired, ET.ParseError, ZeroDivisionError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
