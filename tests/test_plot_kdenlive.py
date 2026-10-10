# SPDX-License-Identifier: MIT
# Copyright (c) 2026 r1317

import contextlib
from fractions import Fraction
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import plot_kdenlive as app


def fixture(path):
    """独立した最小プロジェクト。既存の BGM と背景を含む。"""
    root = ET.Element("mlt", root=".", producer="main_bin")
    ET.SubElement(root, "profile", frame_rate_num="24", frame_rate_den="1", width="3840", height="2160")
    black = ET.SubElement(root, "producer", id="black", out="23")
    app.set_prop(black, "kdenlive:playlistid", "black_track")
    background = ET.SubElement(root, "producer", id="background", out="23")
    app.set_prop(background, "kdenlive:id", "10")
    app.set_prop(background, "resource", "background.png")
    for index, (name, audio, occupied) in enumerate([
        ("voice", True, False), ("BGM", True, True), ("背景", False, True),
        ("立ち絵", False, False), ("字幕", False, False),
    ]):
        for side in range(2):
            playlist = ET.SubElement(root, "playlist", id=f"playlist{index * 2 + side}")
            if occupied and side == 0:
                ET.SubElement(playlist, "entry", producer="background", **{"in": "0", "out": "23"})
        track = ET.SubElement(root, "tractor", id=f"tractor{index}", out="23")
        app.set_prop(track, "kdenlive:track_name", name)
        if audio:
            app.set_prop(track, "kdenlive:audio_track", "1")
        for side in range(2):
            ET.SubElement(track, "track", producer=f"playlist{index * 2 + side}", hide="video" if audio else "audio")
        if name == "立ち絵":
            effect = ET.SubElement(track, "filter", id="filter0")
            app.set_prop(effect, "mlt_service", "qtblend")
            app.set_prop(effect, "rect", "0=530 100 4992 2808 1")
    sequence = ET.SubElement(root, "tractor", id="sequence", out="23")
    app.set_prop(sequence, "kdenlive:uuid", "{sequence}")
    app.set_prop(sequence, "kdenlive:sequenceproperties.groups", "[]")
    ET.SubElement(sequence, "track", producer="black")
    for index in range(5):
        ET.SubElement(sequence, "track", producer=f"tractor{index}")
    main_bin = ET.SubElement(root, "playlist", id="main_bin")
    app.set_prop(main_bin, "kdenlive:docproperties.activetimeline", "{sequence}")
    ET.SubElement(main_bin, "entry", producer="sequence", **{"in": "0", "out": "23"})
    wrapper = ET.SubElement(root, "tractor", id="wrapper", out="23")
    app.set_prop(wrapper, "kdenlive:projectTractor", "1")
    ET.SubElement(wrapper, "track", producer="sequence", out="23")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def media(path, length):
    return app.Media(path, length, {"index": 0, "width": 1500, "height": 2000}, {"index": 1})


def assert_references(test, root):
    ids = [el.get("id") for el in root.iter() if el.get("id")]
    test.assertEqual(len(ids), len(set(ids)))
    defined = set()
    for element in root:
        for child in element.iter():
            if child.tag in ("entry", "track"):
                test.assertIn(child.get("producer"), defined)
        if element.get("id"):
            defined.add(element.get("id"))


class ArrangementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "base.kdenlive"
        fixture(self.source)
        self.video_dir = self.directory / "videos"
        self.video_dir.mkdir()
        self.video = self.video_dir / "001_映像.mov"
        self.video.touch()
        self.script = self.directory / "台本.txt"
        self.script.write_text("日本語 <字幕> & 記号\n", encoding="utf-8")

    def build(self, lengths=(90, 133)):
        project = app.Project(self.source)
        tracks = [project.select_track("立ち絵", False), project.select_track("A2", True), project.select_track("字幕", False)]
        template = app.title_template(project, None)
        items = [(n, media(self.video, length), f"日本語 <{n}> & 記号") for n, length in enumerate(lengths, 1)]
        app.arrange(project, items, template, *tracks)
        return project, tracks

    def test_gapless_av_subtitles_and_preserved_tracks(self):
        before = app.Project(self.source)
        project, tracks = self.build()
        for track in tracks:
            entries = track.playlists[0].findall("entry")
            self.assertEqual([(e.get("in"), e.get("out")) for e in entries], [("0", "89"), ("0", "132")])
            self.assertIsNone(track.playlists[0].find("blank"))
            self.assertEqual(track.element.get("out"), "222")
        for key in ("playlist2", "playlist3", "tractor1", "playlist4", "playlist5", "tractor2"):
            self.assertEqual(ET.tostring(before.ids[key]), ET.tostring(project.ids[key]))
        self.assertEqual(ET.tostring(before.ids["tractor3"].find("filter")), ET.tostring(project.ids["tractor3"].find("filter")))
        groups = json.loads(app.prop(project.sequence, "kdenlive:sequenceproperties.groups"))
        self.assertEqual([g["type"] for g in groups], ["AVSplit", "AVSplit"])
        self.assertEqual([c["data"] for c in groups[1]["children"]], ["3:90:-1", "0:90:-1"])
        self.assertEqual(project.sequence.get("out"), "222")
        self.assertEqual(project.ids["wrapper"].get("out"), "222")
        self.assertEqual(project.ids["black"].get("out"), "222")
        self.assertEqual(project.root.get("root"), str(self.directory))
        assert_references(self, project.root)
        elements = {e.get("id"): e for e in project.root}
        for video_entry, audio_entry in zip(tracks[0].playlists[0].findall("entry"), tracks[1].playlists[0].findall("entry")):
            video = elements[video_entry.get("producer")]
            audio = elements[audio_entry.get("producer")]
            self.assertEqual(app.prop(video, "kdenlive:id"), app.prop(audio, "kdenlive:id"))
            self.assertEqual(app.prop(video, "set.test_audio"), "1")
            self.assertEqual(app.prop(audio, "set.test_image"), "1")

    def test_title_round_trip_and_duration(self):
        project, tracks = self.build()
        output = self.directory / "result.kdenlive"
        app.write_project(project.tree, output, False)
        root = ET.parse(output).getroot()
        titles = [p for p in root.findall("producer") if app.prop(p, "mlt_service") == "kdenlivetitle"]
        self.assertEqual(len(titles), 2)
        for n, (producer, length) in enumerate(zip(titles, (90, 133)), 1):
            title = ET.fromstring(app.prop(producer, "xmldata"))
            content = title.find("item/content")
            self.assertEqual(content.text, f"日本語 <{n}> & 記号")
            self.assertNotIn("preferred-width", content.attrib)
            self.assertNotIn("box-width", content.attrib)
            self.assertEqual(title.get("duration"), str(length))
            self.assertEqual(app.prop(producer, "kdenlive:clipname"), f"字幕{n:03}")
        assert_references(self, root)

    def test_occupied_secondary_playlist_rejected_without_mutation(self):
        project = app.Project(self.source)
        tracks = [project.select_track("立ち絵", False), project.select_track("A2", True), project.select_track("字幕", False)]
        ET.SubElement(tracks[2].playlists[1], "entry", producer="background", out="0")
        before = ET.tostring(project.root)
        with self.assertRaisesRegex(app.ProjectError, "空ではありません"):
            app.arrange(project, [(1, media(self.video, 5), "字幕")], app.title_template(project, None), *tracks)
        self.assertEqual(ET.tostring(project.root), before)

    def test_existing_output_and_input_protected(self):
        project, _ = self.build()
        output = self.directory / "result.kdenlive"
        output.write_text("existing", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            app.write_project(project.tree, output, False)
        self.assertEqual(output.read_text(), "existing")
        before = self.source.read_bytes()
        args = app.parser().parse_args([str(self.source), "--videos", str(self.video_dir), "--script", str(self.script), "-o", str(self.source), "--force"])
        with self.assertRaisesRegex(app.ProjectError, "入力ファイルと異なる"):
            app.run(args)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(list(self.directory.glob(".arrange-*.tmp")))

    def test_dry_run_does_not_write(self):
        args = app.parser().parse_args([str(self.source), "--videos", str(self.video_dir), "--script", str(self.script), "--dry-run"])
        with patch.object(app, "probe_media", return_value=media(self.video, 90)), contextlib.redirect_stdout(io.StringIO()):
            app.run(args)
        self.assertFalse((self.directory / "base-arranged.kdenlive").exists())

    def test_numeric_order_bom_and_unnumbered_files(self):
        for n in range(2, 11):
            (self.video_dir / f"{n}_映像.MOV").touch()
        (self.video_dir / "問題文.mov").touch()
        self.script.write_text("\n".join(f"字幕{n}" for n in range(1, 11)), encoding="utf-8-sig")
        items = app.read_inputs(self.video_dir, self.script, "utf-8-sig")
        self.assertEqual([n for n, _, _ in items], list(range(1, 11)))
        self.assertEqual(items[0][2], "字幕1")

    def test_duplicate_missing_and_blank_lines_rejected(self):
        duplicate = self.video_dir / "1_duplicate.mp4"
        duplicate.touch()
        with self.assertRaisesRegex(app.ProjectError, "重複"):
            app.read_inputs(self.video_dir, self.script, "utf-8")
        duplicate.unlink()
        self.script.write_text("first\n\nthird", encoding="utf-8")
        with self.assertRaisesRegex(app.ProjectError, "2 行目が空"):
            app.read_inputs(self.video_dir, self.script, "utf-8")
        self.script.write_text("first\nsecond", encoding="utf-8")
        with self.assertRaisesRegex(app.ProjectError, "動画がない行: 002"):
            app.read_inputs(self.video_dir, self.script, "utf-8")
        self.script.write_text("first", encoding="utf-8")
        (self.video_dir / "002_more.mov").touch()
        with self.assertRaisesRegex(app.ProjectError, "台本の行がない動画: 002"):
            app.read_inputs(self.video_dir, self.script, "utf-8")

    def test_short_insertion_does_not_shorten_other_tracks(self):
        project, _ = self.build((5,))
        self.assertEqual(project.sequence.get("out"), "23")

    def test_probe_uses_video_duration_not_audio_padding(self):
        data = {"streams": [
            {"index": 0, "codec_type": "video", "duration_ts": 46080, "time_base": "1/12288", "width": 1500, "height": 2000},
            {"index": 1, "codec_type": "audio"},
        ], "format": {"duration": "3.80"}}
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(data), stderr="")
        with patch.object(app.subprocess, "run", return_value=result):
            self.assertEqual(app.probe_media(self.video, Fraction(24), "ffprobe").length, 90)
        data["streams"][0]["duration_ts"] = "N/A"
        data["streams"][0]["duration"] = "3.75"
        result.stdout = json.dumps(data)
        with patch.object(app.subprocess, "run", return_value=result):
            self.assertEqual(app.probe_media(self.video, Fraction(24), "ffprobe").length, 90)
        data["streams"].pop()
        result.stdout = json.dumps(data)
        with patch.object(app.subprocess, "run", return_value=result), self.assertRaisesRegex(app.ProjectError, "映像と音声"):
            app.probe_media(self.video, Fraction(24), "ffprobe")

    def test_fractional_frame_rate_round_trip(self):
        for fps in (Fraction(24), Fraction(30000, 1001)):
            for count in (0, 1, 90, 123456):
                self.assertEqual(app.frames(app.clock_time(count, fps), fps), count)
        self.assertEqual(app.frames("00:00:03.708", Fraction(24)), 89)
        self.assertEqual(app.frames("00:00:03:17", Fraction(24)), 89)


class StyleTests(unittest.TestCase):
    def test_style_from_separate_project_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            target_path = directory / "target.kdenlive"
            style_path = directory / "style.kdenlive"
            fixture(target_path)
            fixture(style_path)
            style_root = ET.parse(style_path).getroot()
            template = ET.Element("kdenlivetitle", width="3840", height="2160")
            item = ET.SubElement(template, "item", type="QGraphicsTextItem")
            ET.SubElement(item, "position", x="46", y="1790")
            ET.SubElement(item, "content", font="Example Font", **{"font-color": "255,255,255,255", "box-width": "300"}).text = "old text"
            title = ET.Element("producer", id="style-title")
            app.set_prop(title, "kdenlive:clipname", "字幕001")
            app.set_prop(title, "mlt_service", "kdenlivetitle")
            app.set_prop(title, "xmldata", ET.tostring(template, encoding="unicode"))
            style_root.insert(1, title)
            ET.ElementTree(style_root).write(style_path, encoding="utf-8")

            target = app.Project(target_path)
            extracted = app.title_template(target, style_path)
            self.assertEqual(extracted.find("item/content").get("font"), "Example Font")
            producer = app.make_title(target, extracted, "new text", 1, 90, target.bin_id())
            result = ET.fromstring(app.prop(producer, "xmldata"))
            self.assertEqual(result.find("item/content").text, "new text")
            self.assertEqual(result.find("item/content").get("font"), "Example Font")
            self.assertEqual(result.find("item/position").attrib, {"x": "46", "y": "1790"})
            self.assertNotIn("box-width", result.find("item/content").attrib)


if __name__ == "__main__":
    unittest.main()
