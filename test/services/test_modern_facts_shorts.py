import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts import generate_shorts_backgrounds as backgrounds
from scripts import run_modern_facts_shorts as shorts


class TestEnsureConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-config-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.example = self.tmp / "config.example.toml"
        self.example.write_text(
            '[app]\nllm_provider = "moonshot"\ngemini_api_key = ""\n'
            'subtitle_provider = "edge"\nvideo_source = "pexels"\n',
            encoding="utf-8",
        )
        self.config = self.tmp / "config.toml"

    def test_creates_config_from_example_and_applies_fields(self):
        changes = shorts.ensure_config(
            self.config,
            self.example,
            llm_provider="gemini",
            gemini_api_key="secret-key",
        )

        self.assertTrue(self.config.is_file())
        self.assertIn("created config.toml from config.example.toml", changes)

        import toml

        app = toml.load(str(self.config))["app"]
        self.assertEqual(app["llm_provider"], "gemini")
        self.assertEqual(app["gemini_api_key"], "secret-key")
        self.assertEqual(app["subtitle_provider"], "edge")
        self.assertEqual(app["video_source"], "pexels")

    def test_second_run_with_same_values_makes_no_changes(self):
        shorts.ensure_config(
            self.config, self.example, llm_provider="gemini", gemini_api_key="k"
        )
        before = self.config.read_bytes()

        changes = shorts.ensure_config(
            self.config, self.example, llm_provider="gemini", gemini_api_key="k"
        )

        self.assertEqual(changes, [])
        self.assertEqual(self.config.read_bytes(), before)

    def test_missing_example_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            shorts.ensure_config(self.config, self.tmp / "nope.toml")

    def test_empty_values_leave_existing_config_alone(self):
        self.example.write_text(
            '[app]\nllm_provider = "moonshot"\n', encoding="utf-8"
        )
        shutil.copyfile(self.example, self.config)
        before = self.config.read_bytes()

        changes = shorts.ensure_config(self.config, self.example)

        self.assertEqual(changes, [])
        self.assertEqual(self.config.read_bytes(), before)

        import toml

        app = toml.load(str(self.config))["app"]
        self.assertEqual(app["llm_provider"], "moonshot")


class TestTopicSelection(unittest.TestCase):
    def test_prompt_mentions_channel(self):
        prompt = shorts.build_topic_prompt("Modern Facts")
        self.assertIn("Modern Facts", prompt)
        self.assertIn("ONE topic", prompt)

    def test_parse_topic_strips_fences_bullets_and_labels(self):
        fenced = "```text\n- Topic: Neural Chips Beat Human Reflexes\n```"
        self.assertEqual(
            shorts.parse_topic_response(fenced),
            "Neural Chips Beat Human Reflexes",
        )

    def test_parse_topic_rejects_errors_and_empty_replies(self):
        self.assertEqual(shorts.parse_topic_response("Error: quota exceeded"), "")
        self.assertEqual(shorts.parse_topic_response(""), "")
        self.assertEqual(shorts.parse_topic_response(None), "")
        self.assertEqual(shorts.parse_topic_response("   \n  "), "")

    def test_pick_topic_retries_then_succeeds(self):
        replies = iter(["Error: transient", '"Robots Now Build Bridges"'])

        topic = shorts.pick_topic("prompt", generate=lambda prompt: next(replies))

        self.assertEqual(topic, "Robots Now Build Bridges")

    def test_pick_topic_raises_after_exhausted_attempts(self):
        with self.assertRaises(RuntimeError):
            shorts.pick_topic(
                "prompt", generate=lambda prompt: "Error: nope", attempts=2
            )


class TestSubjectDerivation(unittest.TestCase):
    def test_first_sentence_becomes_the_subject(self):
        subject = shorts.derive_subject(
            "AI agents now run entire data centers. This changes everything."
        )

        self.assertEqual(subject, "AI agents now run entire data centers")

    def test_empty_script_falls_back_to_a_placeholder(self):
        self.assertEqual(shorts.derive_subject("   "), "Modern Facts short")

    def test_long_sentence_is_truncated(self):
        subject = shorts.derive_subject("word " * 50)

        self.assertLessEqual(len(subject), 100)


class TestBuildCliCommand(unittest.TestCase):
    def test_local_materials_and_voice_flags(self):
        command = shorts.build_cli_command(
            python="python",
            cli_path=Path("cli.py"),
            subject="AI topic",
            materials=[Path("a.mp4"), Path("b.mp4")],
            voice_name="en-US-EmmaMultilingualNeural-Female",
            voice_rate=1.25,
            language="en-US",
            paragraph_number=3,
            script_prompt="keep it short",
        )

        joined = " ".join(command)
        self.assertEqual(command[0], "python")
        self.assertIn("--video-subject AI topic", joined)
        self.assertIn("--video-source local", joined)
        self.assertIn("--video-materials a.mp4,b.mp4", joined)
        self.assertIn("--voice-name en-US-EmmaMultilingualNeural-Female", joined)
        self.assertIn("--voice-rate 1.25", joined)
        self.assertIn("--paragraph-number 3", joined)
        self.assertIn("--video-script-prompt keep it short", joined)
        self.assertIn("--video-aspect 9:16", joined)
        self.assertIn("--stop-at video", joined)
        self.assertNotIn("--video-script", command)

    def test_script_mode_adds_video_script(self):
        command = shorts.build_cli_command(
            python="python",
            cli_path=Path("cli.py"),
            subject="AI topic",
            script="Hook sentence. More text.",
            materials=[Path("a.mp4")],
        )

        joined = " ".join(command)
        self.assertIn("--video-script Hook sentence. More text.", joined)
        self.assertIn("--video-subject AI topic", joined)

    def test_extra_args_are_appended_verbatim(self):
        command = shorts.build_cli_command(
            python="python",
            cli_path=Path("cli.py"),
            subject="x",
            materials=[Path("a.mp4")],
            extra_args=["--video-count", "2"],
        )

        self.assertEqual(command[-2:], ["--video-count", "2"])


class TestRunCliTask(unittest.TestCase):
    def test_parses_json_result_line(self):
        payload = {
            "task_id": "abc",
            "result": {"state": "completed", "videos": ["storage/tasks/abc/final-1.mp4"]},
        }
        completed = MagicMock(
            returncode=0,
            stdout=f"log line\n{json.dumps(payload)}\n",
            stderr="",
        )
        with patch.object(shorts.subprocess, "run", return_value=completed):
            result = shorts.run_cli_task(["python", "cli.py"])

        self.assertEqual(result["task_id"], "abc")
        self.assertEqual(result["videos"], ["storage/tasks/abc/final-1.mp4"])

    def test_failure_raises_with_stderr_tail(self):
        completed = MagicMock(returncode=1, stdout="", stderr="boom\n" * 60)
        with patch.object(shorts.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as raised:
                shorts.run_cli_task(["python", "cli.py"])

        self.assertIn("exit code 1", str(raised.exception))
        self.assertIn("boom", str(raised.exception))

    def test_task_error_state_raises(self):
        payload = {"task_id": "abc", "result": {"state": "failed", "error": "TTS down"}}
        completed = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch.object(shorts.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as raised:
                shorts.run_cli_task(["python", "cli.py"])

        self.assertIn("TTS down", str(raised.exception))

    def test_missing_video_fails_when_video_is_required(self):
        payload = {"task_id": "abc", "result": {"state": "completed", "videos": []}}
        completed = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch.object(shorts.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError):
                shorts.run_cli_task(["python", "cli.py"])

    def test_script_only_stage_is_allowed_without_video(self):
        payload = {"task_id": "abc", "result": {"state": "completed", "videos": []}}
        completed = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch.object(shorts.subprocess, "run", return_value=completed):
            result = shorts.run_cli_task(
                ["python", "cli.py"], require_video=False
            )

        self.assertEqual(result["task_id"], "abc")
        self.assertEqual(result["videos"], [])


class TestOutputCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-out-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.task_dir = self.tmp / "task"
        self.task_dir.mkdir()
        (self.task_dir / "final-1.mp4").write_bytes(b"mp4-bytes")
        (self.task_dir / "subtitle.srt").write_text("1\n", encoding="utf-8")
        (self.task_dir / "script.json").write_text(
            json.dumps({"script": "Hook."}), encoding="utf-8"
        )
        self.output_dir = self.tmp / "modern_facts"

    def test_copies_artifacts_and_writes_meta(self):
        with patch("app.utils.utils.task_dir", return_value=str(self.task_dir)):
            meta = shorts.collect_outputs(
                task_id="tid",
                videos=[str(self.task_dir / "final-1.mp4")],
                output_dir=self.output_dir,
                subject="Robots Build Bridges",
                script_text="Hook.",
                voice_name="en-US-AndrewMultilingualNeural-Male",
            )

        videos = meta["outputs"]["videos"]
        self.assertEqual(len(videos), 1)
        video_path = Path(videos[0])
        self.assertTrue(video_path.is_file())
        self.assertTrue(video_path.name.endswith("-robots-build-bridges.mp4"))
        self.assertEqual(video_path.read_bytes(), b"mp4-bytes")
        self.assertTrue(Path(meta["outputs"]["subtitle.srt"][0]).is_file())
        self.assertTrue(Path(meta["outputs"]["script.json"][0]).is_file())

        meta_path = Path(meta["outputs"]["meta"][0])
        stored = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["subject"], "Robots Build Bridges")
        self.assertEqual(stored["script"], "Hook.")


class TestBackgroundHelpers(unittest.TestCase):
    def test_every_preset_builds_a_valid_command(self):
        for preset in backgrounds.BACKGROUND_PRESETS:
            command = backgrounds.build_ffmpeg_command(
                preset,
                Path("out.mp4"),
                ffmpeg="ffmpeg",
                duration=12.0,
                seed=3,
            )
            joined = " ".join(command)
            self.assertIn("-f lavfi", joined)
            self.assertIn("-t 12", joined)
            self.assertTrue(str(command[-1]).endswith("out.mp4"))
            if preset["filters"]:
                self.assertIn("-vf", command)

    def test_existing_backgrounds_only_lists_generated_clips(self):
        tmp = Path(tempfile.mkdtemp(prefix="shorts-bgs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "background-01-deep-space-blue.mp4").write_bytes(b"x")
        (tmp / "notes.txt").write_bytes(b"x")

        found = backgrounds.existing_backgrounds(tmp)

        self.assertEqual([path.name for path in found], ["background-01-deep-space-blue.mp4"])

    def test_generate_reuses_existing_clips_without_ffmpeg(self):
        tmp = Path(tempfile.mkdtemp(prefix="shorts-bgs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for index in range(3):
            (tmp / f"background-{index + 1:02d}-plasma-violet.mp4").write_bytes(b"x")

        with patch.object(backgrounds, "find_ffmpeg") as find_ffmpeg:
            paths = backgrounds.generate_backgrounds(tmp, count=3, duration=4.0)

        find_ffmpeg.assert_not_called()
        self.assertEqual(len(paths), 3)

    def test_generate_renders_missing_clips(self):
        tmp = Path(tempfile.mkdtemp(prefix="shorts-bgs-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"mp4")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(backgrounds, "find_ffmpeg", return_value="ffmpeg"), patch.object(
            backgrounds.subprocess, "run", side_effect=fake_run
        ):
            paths = backgrounds.generate_backgrounds(
                tmp, count=2, duration=4.0, log=lambda message: None
            )

        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.is_file() for path in paths))

    def test_invalid_count_is_rejected(self):
        with self.assertRaises(ValueError):
            backgrounds.generate_backgrounds(Path("unused"), count=0)


class TestResolveMaterials(unittest.TestCase):
    def test_explicit_materials_are_resolved(self):
        tmp = Path(tempfile.mkdtemp(prefix="shorts-mat-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        clip = tmp / "clip.mp4"
        clip.write_bytes(b"x")
        args = MagicMock(materials=str(clip), stop_at="video")

        paths = shorts.resolve_materials(args, lambda message: None)

        self.assertEqual(paths, [clip.resolve()])

    def test_missing_explicit_material_fails_fast(self):
        args = MagicMock(materials="does-not-exist.mp4", stop_at="video")
        with self.assertRaises(FileNotFoundError):
            shorts.resolve_materials(args, lambda message: None)


class TestRedaction(unittest.TestCase):
    def test_key_bearing_argument_is_redacted(self):
        redacted = shorts._redact(
            ["python", "--gemini_api_key=secret", "--voice-rate", "1.1"]
        )

        self.assertEqual(redacted[1], "<redacted>")
        self.assertNotIn("secret", " ".join(redacted))

    def test_upload_credentials_are_redacted(self):
        redacted = shorts._redact(
            ["--upload_post_api_key=up-secret", "--upload_post_username=bob"]
        )

        self.assertEqual(redacted, ["<redacted>", "<redacted>"])


class TestEnsureConfigUpload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-upl-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.example = self.tmp / "config.example.toml"
        self.example.write_text(
            '[app]\nllm_provider = "moonshot"\n'
            'upload_post_enabled = false\nupload_post_auto_upload = false\n'
            'upload_post_api_key = ""\nupload_post_username = ""\n'
            'upload_post_platforms = ["tiktok", "instagram"]\n'
            'upload_post_youtube_privacy_status = "public"\n',
            encoding="utf-8",
        )
        self.config = self.tmp / "config.toml"

    def _load_app(self) -> dict:
        import toml

        return toml.load(str(self.config))["app"]

    def test_enabling_upload_wires_every_pipeline_field(self):
        changes = shorts.ensure_config(
            self.config,
            self.example,
            upload_api_key="up-secret-key",
            upload_username="modernfacts",
            upload_platforms=["youtube"],
            upload_privacy="private",
            upload_enabled=True,
        )

        app = self._load_app()
        self.assertTrue(app["upload_post_enabled"])
        self.assertTrue(app["upload_post_auto_upload"])
        self.assertEqual(app["upload_post_api_key"], "up-secret-key")
        self.assertEqual(app["upload_post_username"], "modernfacts")
        self.assertEqual(app["upload_post_platforms"], ["youtube"])
        self.assertEqual(app["upload_post_youtube_privacy_status"], "private")
        self.assertEqual(len(changes), 7)

    def test_change_labels_never_contain_secret_values(self):
        changes = shorts.ensure_config(
            self.config,
            self.example,
            upload_api_key="up-secret-key",
            upload_username="modernfacts",
            upload_platforms=["youtube"],
            upload_privacy="private",
            upload_enabled=True,
        )

        joined = " ".join(changes)
        self.assertNotIn("up-secret-key", joined)
        self.assertNotIn("modernfacts", joined)

    def test_second_run_with_same_upload_values_makes_no_changes(self):
        kwargs = dict(
            upload_api_key="up-secret-key",
            upload_username="modernfacts",
            upload_platforms=["youtube"],
            upload_privacy="private",
            upload_enabled=True,
        )
        shorts.ensure_config(self.config, self.example, **kwargs)
        before = self.config.read_bytes()

        changes = shorts.ensure_config(self.config, self.example, **kwargs)

        self.assertEqual(changes, [])
        self.assertEqual(self.config.read_bytes(), before)

    def test_no_upload_forces_both_booleans_off(self):
        shorts.ensure_config(self.config, self.example, upload_enabled=True)
        shorts.ensure_config(self.config, self.example, upload_enabled=False)

        app = self._load_app()
        self.assertFalse(app["upload_post_enabled"])
        self.assertFalse(app["upload_post_auto_upload"])

    def test_upload_enabled_none_leaves_existing_upload_config_alone(self):
        shorts.ensure_config(
            self.config,
            self.example,
            upload_api_key="up-secret-key",
            upload_username="modernfacts",
            upload_platforms=["youtube"],
            upload_privacy="unlisted",
            upload_enabled=True,
        )
        before = self.config.read_bytes()

        changes = shorts.ensure_config(self.config, self.example)

        self.assertEqual(changes, [])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self._load_app()["upload_post_youtube_privacy_status"], "unlisted")

    def test_force_disable_from_fresh_example(self):
        shorts.ensure_config(self.config, self.example, upload_enabled=False)

        self.assertFalse(self._load_app()["upload_post_enabled"])


class TestReadUploadSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-upl-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / "config.toml"

    def test_missing_config_is_reported_as_not_configured(self):
        settings = shorts.read_upload_settings(self.config)

        self.assertFalse(settings["configured"])
        self.assertEqual(settings["privacy"], "public")

    def test_full_gate_matches_the_pipeline(self):
        self.config.write_text(
            "[app]\n"
            "upload_post_enabled = true\n"
            "upload_post_auto_upload = true\n"
            'upload_post_api_key = "k"\n'
            'upload_post_username = "u"\n'
            'upload_post_platforms = ["youtube"]\n'
            'upload_post_youtube_privacy_status = "private"\n',
            encoding="utf-8",
        )

        settings = shorts.read_upload_settings(self.config)

        self.assertTrue(settings["configured"])
        self.assertEqual(settings["platforms"], ["youtube"])
        self.assertEqual(settings["privacy"], "private")

    def test_auto_upload_off_means_not_configured(self):
        self.config.write_text(
            "[app]\n"
            "upload_post_enabled = true\n"
            "upload_post_auto_upload = false\n"
            'upload_post_api_key = "k"\n'
            'upload_post_username = "u"\n',
            encoding="utf-8",
        )

        self.assertFalse(shorts.read_upload_settings(self.config)["configured"])

    def test_empty_platforms_means_not_configured(self):
        self.config.write_text(
            "[app]\n"
            "upload_post_enabled = true\n"
            "upload_post_auto_upload = true\n"
            'upload_post_api_key = "k"\n'
            'upload_post_username = "u"\n'
            "upload_post_platforms = []\n",
            encoding="utf-8",
        )

        self.assertFalse(shorts.read_upload_settings(self.config)["configured"])


class TestUploadReport(unittest.TestCase):
    def _settings(self, configured: bool = True) -> dict:
        return {
            "configured": configured,
            "platforms": ["youtube"],
            "privacy": "private",
        }

    def test_completed_cross_post_is_uploaded(self):
        report = shorts.build_upload_report(
            {"cross_post_state": "complete", "cross_post_results": [{"youtube": "ok"}]},
            task_id="tid",
            upload_settings=self._settings(),
        )

        self.assertEqual(report["status"], "uploaded")
        self.assertIsNone(report["reason"])
        self.assertEqual(report["youtube_privacy_status"], "private")
        self.assertEqual(report["results"], [{"youtube": "ok"}])

    def test_failed_cross_post_reports_the_error(self):
        report = shorts.build_upload_report(
            {"cross_post_state": "failed", "cross_post_error": "upload rejected"},
            task_id="tid",
            upload_settings=self._settings(),
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason"], "upload rejected")

    def test_still_pending_after_timeout_is_unconfirmed(self):
        for state_value in ("pending", "processing"):
            report = shorts.build_upload_report(
                {"cross_post_state": state_value},
                task_id="tid",
                upload_settings=self._settings(),
            )

            self.assertEqual(report["status"], "failed")
            self.assertIn("unconfirmed", report["reason"])

    def test_missing_state_despite_config_is_failed(self):
        report = shorts.build_upload_report(
            {},
            task_id="tid",
            upload_settings=self._settings(),
        )

        self.assertEqual(report["status"], "failed")
        self.assertIn("not scheduled", report["reason"])

    def test_unconfigured_is_disabled_with_secrets_hint(self):
        report = shorts.build_upload_report(
            {},
            task_id="tid",
            upload_settings=self._settings(configured=False),
        )

        self.assertEqual(report["status"], "disabled")
        self.assertIn("UPLOAD_POST_API_KEY", report["reason"])
        self.assertIn("UPLOAD_POST_USERNAME", report["reason"])

    def test_early_stop_is_skipped(self):
        report = shorts.build_upload_report(
            {},
            task_id="tid",
            upload_settings=self._settings(),
            stop_at="script",
        )

        self.assertEqual(report["status"], "skipped")

    def test_report_never_contains_credentials(self):
        report = shorts.build_upload_report(
            {"cross_post_state": "failed", "cross_post_error": "bad token"},
            task_id="tid",
            upload_settings=self._settings(),
        )

        self.assertNotIn("upload_post_api_key", json.dumps(report))
        self.assertNotIn("upload_post_username", json.dumps(report))


class TestUploadStatusFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-st-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_write_round_trips_and_creates_directory(self):
        report = {"status": "uploaded", "reason": None, "platforms": ["youtube"]}
        nested = self.tmp / "a" / "b"

        path = shorts.write_upload_status(nested, report)

        self.assertEqual(path.name, "upload-status.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)


class TestVerifyUploadStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-ver-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "upload-status.json"

    def _write(self, payload) -> None:
        if isinstance(payload, str):
            self.path.write_text(payload, encoding="utf-8")
        else:
            self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file_passes(self):
        with patch("sys.stdout", new=MagicMock()) as stdout:
            code = shorts.verify_upload_status(self.path)

        self.assertEqual(code, 0)
        stdout.write.assert_called()

    def test_uploaded_disabled_and_skipped_pass(self):
        for status in ("uploaded", "disabled", "skipped"):
            with self.subTest(status=status):
                self._write({"status": status, "reason": None})
                self.assertEqual(shorts.verify_upload_status(self.path), 0)

    def test_failed_returns_one_with_reason_on_stderr(self):
        self._write({"status": "failed", "reason": "quota exceeded"})
        stderr = io.StringIO()

        with patch("sys.stderr", stderr):
            code = shorts.verify_upload_status(self.path)

        self.assertEqual(code, 1)
        self.assertIn("YouTube upload failed: quota exceeded", stderr.getvalue())

    def test_github_actions_emits_error_annotation(self):
        self._write({"status": "failed", "reason": "boom"})
        stdout = io.StringIO()

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), patch(
            "sys.stdout", stdout
        ), patch("sys.stderr", io.StringIO()):
            code = shorts.verify_upload_status(self.path)

        self.assertEqual(code, 1)
        self.assertIn("::error::YouTube upload failed: boom", stdout.getvalue())

    def test_corrupt_status_file_returns_one(self):
        self._write("{not json")
        stderr = io.StringIO()

        with patch("sys.stderr", stderr):
            code = shorts.verify_upload_status(self.path)

        self.assertEqual(code, 1)
        self.assertIn("could not read upload status", stderr.getvalue())


class TestStepSummary(unittest.TestCase):
    def test_no_env_var_is_a_no_op(self):
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            shorts.append_step_summary({"status": "uploaded"})

    def test_appends_credential_free_summary(self):
        summary = Path(tempfile.mkdtemp(prefix="shorts-sum-")) / "summary.md"
        self.addCleanup(shutil.rmtree, summary.parent, True)
        report = {
            "status": "failed",
            "reason": "quota\nexceeded",
            "youtube_privacy_status": "private",
            "platforms": ["youtube"],
        }

        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            shorts.append_step_summary(report)

        text = summary.read_text(encoding="utf-8")
        self.assertIn("## YouTube upload", text)
        self.assertIn("`failed`", text)
        self.assertIn("quota exceeded", text)
        self.assertNotIn("api_key", text)
        self.assertNotIn("api_key", text)


class TestWaitForCrossPost(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.services import task as task_service

        cls.task_service = task_service

    def _fake_state(self, side_effect=None, return_value=None):
        fake = MagicMock()
        if side_effect is not None:
            fake.get_task.side_effect = side_effect
        else:
            fake.get_task.return_value = return_value
        return fake

    def test_terminal_state_returns_immediately(self):
        fake_state = self._fake_state(
            return_value={
                "cross_post_state": "complete",
                "cross_post_results": [{"youtube": "ok"}],
            }
        )

        with patch.object(self.task_service.sm, "state", fake_state):
            result = self.task_service.wait_for_cross_post("tid", timeout=5)

        self.assertEqual(result["cross_post_state"], "complete")
        self.assertEqual(result["cross_post_results"], [{"youtube": "ok"}])
        fake_state.get_task.assert_called_once()

    def test_polls_until_terminal_state(self):
        fake_state = self._fake_state(
            side_effect=[
                {"cross_post_state": "processing"},
                {"cross_post_state": "failed", "cross_post_error": "boom"},
            ]
        )

        with patch.object(self.task_service.sm, "state", fake_state), patch.object(
            self.task_service.time, "sleep"
        ):
            result = self.task_service.wait_for_cross_post("tid", timeout=5)

        self.assertEqual(result["cross_post_state"], "failed")
        self.assertEqual(result["cross_post_error"], "boom")
        self.assertEqual(fake_state.get_task.call_count, 2)

    def test_timeout_reports_unconfirmed(self):
        fake_state = self._fake_state(return_value={"cross_post_state": "processing"})

        with patch.object(self.task_service.sm, "state", fake_state):
            result = self.task_service.wait_for_cross_post("tid", timeout=0)

        self.assertEqual(result["cross_post_state"], "processing")
        self.assertIn("did not finish within 0s", result["cross_post_error"])
        self.assertIn("outcome unconfirmed", result["cross_post_error"])

    def test_absent_task_returns_empty_fields(self):
        fake_state = self._fake_state(return_value=None)

        with patch.object(self.task_service.sm, "state", fake_state):
            result = self.task_service.wait_for_cross_post("missing", timeout=5)

        self.assertIsNone(result["cross_post_state"])
        self.assertIsNone(result["cross_post_error"])


class TestParseUploadArgs(unittest.TestCase):
    def test_upload_defaults(self):
        args = shorts.parse_args([])

        self.assertFalse(args.no_upload)
        self.assertEqual(args.youtube_privacy, "private")
        self.assertEqual(args.upload_platforms, "youtube")
        self.assertEqual(args.cross_post_timeout, 3600.0)
        self.assertEqual(args.verify_upload_status, "")

    def test_upload_flags(self):
        args = shorts.parse_args(
            [
                "--no-upload",
                "--youtube-privacy",
                "unlisted",
                "--upload-platforms",
                "youtube,tiktok",
                "--cross-post-timeout",
                "120",
                "--verify-upload-status",
                "storage/modern_facts/upload-status.json",
            ]
        )

        self.assertTrue(args.no_upload)
        self.assertEqual(args.youtube_privacy, "unlisted")
        self.assertEqual(args.upload_platforms, "youtube,tiktok")
        self.assertEqual(args.cross_post_timeout, 120.0)
        self.assertEqual(
            args.verify_upload_status, "storage/modern_facts/upload-status.json"
        )

    def test_privacy_env_fallback(self):
        with patch.dict(os.environ, {"SHORTS_YOUTUBE_PRIVACY": "unlisted"}):
            args = shorts.parse_args([])

        self.assertEqual(args.youtube_privacy, "unlisted")


if __name__ == "__main__":
    unittest.main()
