import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts import upload_short_youtube as uploader

API_KEY = "test-api-key-SENTINEL"
USERNAME = "test-username-SENTINEL"


class UploadShortYouTubeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shorts-upload-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def _env(self, **overrides) -> dict:
        env = {"UPLOAD_POST_API_KEY": API_KEY, "UPLOAD_POST_USERNAME": USERNAME}
        env.update(overrides)
        return env

    def _run(self, argv, env, github_actions: bool = False):
        environ = dict(os.environ)
        environ.update(env)
        environ["GITHUB_ACTIONS"] = "true" if github_actions else "false"
        with patch.dict(os.environ, environ, clear=True), redirect_stdout(
            self.out
        ), redirect_stderr(self.err):
            code = uploader.main(argv)
        return code, self.out.getvalue(), self.err.getvalue()

    def _make_video(self, name="modern-facts-20261008-120000-ai-topic.mp4", meta=None):
        video = self.tmp / name
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
        if meta is not None:
            meta_path = video.with_name(video.stem + "-meta.json")
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return video

    def assert_no_secrets(self, *texts: str) -> None:
        joined = "\n".join(texts)
        self.assertNotIn(API_KEY, joined)
        self.assertNotIn(USERNAME, joined)


class TestAccountLoading(UploadShortYouTubeTestCase):
    def test_missing_secrets_fails_with_names_only(self):
        code, out, err = self._run([], {"UPLOAD_POST_API_KEY": "", "UPLOAD_POST_USERNAME": ""})

        self.assertEqual(code, 1)
        self.assertIn("UPLOAD_POST_API_KEY", err)
        self.assertIn("UPLOAD_POST_USERNAME", err)
        self.assert_no_secrets(out, err)

    def test_partial_secrets_reports_only_the_missing_name(self):
        code, out, err = self._run([], self._env(UPLOAD_POST_USERNAME=""))

        self.assertEqual(code, 1)
        self.assertIn(
            "missing required environment variable(s): UPLOAD_POST_USERNAME;",
            err,
        )
        self.assertNotIn("UPLOAD_POST_API_KEY", err)
        self.assert_no_secrets(out, err)

    def test_gha_annotation_emitted_for_missing_secrets(self):
        code, out, err = self._run(
            [], {"UPLOAD_POST_API_KEY": ""}, github_actions=True
        )

        self.assertEqual(code, 1)
        self.assertIn("::error::missing required environment variable(s)", out)
        self.assert_no_secrets(out, err)

    def test_valid_account_contains_expected_keys(self):
        with patch.dict(os.environ, self._env()):
            account, error = uploader.load_upload_account()

        self.assertIsNone(error)
        self.assertEqual(account["upload_post_api_key"], API_KEY)
        self.assertEqual(account["upload_post_username"], USERNAME)
        self.assertTrue(account["upload_post_enabled"])


class TestVideoDiscovery(UploadShortYouTubeTestCase):
    def test_newest_mp4_wins(self):
        older = self.tmp / "older.mp4"
        newer = self.tmp / "newer.mp4"
        older.write_bytes(b"a")
        newer.write_bytes(b"b")
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        (self.tmp / "not-a-video.txt").write_text("x", encoding="utf-8")

        found = uploader.find_latest_video(self.tmp)

        self.assertEqual(found, newer)

    def test_empty_directory_returns_none(self):
        self.assertIsNone(uploader.find_latest_video(self.tmp))

    def test_metadata_comes_from_sibling_meta_file(self):
        video = self._make_video(
            meta={"subject": "AI topic", "script": "Hook sentence."}
        )

        metadata = uploader.read_video_metadata(video)

        self.assertEqual(metadata["subject"], "AI topic")

    def test_missing_metadata_is_empty(self):
        video = self._make_video()

        self.assertEqual(uploader.read_video_metadata(video), {})


class TestMainUploadFlow(UploadShortYouTubeTestCase):
    def test_success_uploads_newest_video_with_private_privacy(self):
        video = self._make_video(
            meta={"subject": "AI topic", "script": "Hook sentence."}
        )
        captured = {}

        def fake_cross_post(**kwargs):
            captured.update(kwargs)
            return {"success": True, "request_id": "rid-42"}

        with patch(
            "app.services.upload_post.cross_post_video", side_effect=fake_cross_post
        ):
            code, out, err = self._run(
                ["--video-dir", str(self.tmp)], self._env()
            )

        self.assertEqual(code, 0)
        self.assertIn("uploading", err)
        self.assertIn("privacy: private", err)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "uploaded")
        self.assertEqual(payload["privacy"], "private")
        self.assertEqual(payload["request_id"], "rid-42")

        self.assertEqual(captured["platforms"], ["youtube"])
        self.assertEqual(captured["account"]["upload_post_api_key"], API_KEY)
        self.assertEqual(captured["account"]["upload_post_username"], USERNAME)
        self.assertEqual(captured["video_path"], str(video))
        self.assertEqual(captured["title"], "AI topic")
        extra = captured["youtube_extra"]
        self.assertEqual(extra["privacyStatus"], "private")
        self.assertIs(extra["selfDeclaredMadeForKids"], False)
        self.assertEqual(extra["youtube_title"], "AI topic")
        self.assertEqual(extra["youtube_description"], "Hook sentence.")

        self.assert_no_secrets(out, err)

    def test_privacy_input_is_passed_through(self):
        self._make_video(meta={"subject": "AI topic"})
        captured = {}

        def fake_cross_post(**kwargs):
            captured.update(kwargs)
            return {"success": True, "request_id": "rid"}

        with patch(
            "app.services.upload_post.cross_post_video", side_effect=fake_cross_post
        ):
            code, _, _ = self._run(
                ["--video-dir", str(self.tmp), "--privacy", "unlisted"],
                self._env(),
            )

        self.assertEqual(code, 0)
        self.assertEqual(captured["youtube_extra"]["privacyStatus"], "unlisted")

    def test_explicit_video_flag_skips_discovery(self):
        video = self._make_video(name="explicit.mp4")
        captured = {}

        def fake_cross_post(**kwargs):
            captured.update(kwargs)
            return {"success": True, "request_id": "rid"}

        with patch(
            "app.services.upload_post.cross_post_video", side_effect=fake_cross_post
        ):
            code, _, _ = self._run(
                ["--video", str(video), "--video-dir", str(self.tmp / "unused")],
                self._env(),
            )

        self.assertEqual(code, 0)
        self.assertEqual(captured["video_path"], str(video))

    def test_api_error_fails_step_with_clear_message(self):
        self._make_video(meta={"subject": "AI topic"})

        with patch(
            "app.services.upload_post.cross_post_video",
            return_value={
                "success": False,
                "error": "Upload-Post failed or skipped platforms: youtube",
                "request_id": "rid-9",
            },
        ):
            code, out, err = self._run(
                ["--video-dir", str(self.tmp)],
                self._env(),
                github_actions=True,
            )

        self.assertEqual(code, 1)
        self.assertIn("YouTube upload failed:", err)
        self.assertIn("Upload-Post failed or skipped platforms: youtube", err)
        self.assertIn("rid-9", err)
        self.assertIn("::error::YouTube upload failed:", out)
        self.assertNotIn('"status": "uploaded"', out)
        self.assert_no_secrets(out, err)

    def test_http_exception_is_reported_without_credentials(self):
        import requests

        response = requests.models.Response()
        response.status_code = 401
        error = requests.exceptions.HTTPError(
            "401 Client Error: Unauthorized for url: "
            "https://api.upload-post.com/api/upload",
            response=response,
        )

        self._make_video(meta={"subject": "AI topic"})
        with patch(
            "app.services.upload_post.cross_post_video",
            return_value={"success": False, "error": str(error)},
        ):
            code, out, err = self._run(
                ["--video-dir", str(self.tmp)], self._env(), github_actions=True
            )

        self.assertEqual(code, 1)
        self.assertIn("401 Client Error", err)
        self.assertIn("::error::YouTube upload failed:", out)
        self.assert_no_secrets(out, err)

    def test_invalid_response_is_a_failure(self):
        self._make_video(meta={"subject": "AI topic"})
        with patch(
            "app.services.upload_post.cross_post_video", return_value="not-a-dict"
        ):
            code, out, err = self._run(
                ["--video-dir", str(self.tmp)], self._env(), github_actions=True
            )

        self.assertEqual(code, 1)
        self.assertIn("invalid response", err)
        self.assertIn("::error::", out)

    def test_no_generated_video_fails_clearly(self):
        code, out, err = self._run(
            ["--video-dir", str(self.tmp)], self._env(), github_actions=True
        )

        self.assertEqual(code, 1)
        self.assertIn("no generated video", err)
        self.assertIn("::error::", out)

    def test_missing_explicit_video_fails(self):
        code, _, err = self._run(
            ["--video", str(self.tmp / "nope.mp4")], self._env()
        )

        self.assertEqual(code, 1)
        self.assertIn("video file not found", err)

    def test_default_privacy_is_private(self):
        with patch.dict(os.environ, {}, clear=True):
            args = uploader.parse_args([])

        self.assertEqual(args.privacy, "private")

    def test_privacy_env_fallback(self):
        with patch.dict(
            os.environ, {"SHORTS_YOUTUBE_PRIVACY": "unlisted"}, clear=True
        ):
            args = uploader.parse_args([])

        self.assertEqual(args.privacy, "unlisted")


if __name__ == "__main__":
    unittest.main()
