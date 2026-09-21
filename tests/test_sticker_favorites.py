from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from flask import Flask

from blueprints import media


class StickerFavoritesTest(TestCase):
    def test_uploaded_cos_sticker_persists_after_reload(self):
        sticker_url = "https://example.test/users/7/sticker_uploads/wave.gif"

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            favorites_file = tmp_path / "stickers_favorites.json"

            def fake_get_cos_list(prefix, get_folders=False):
                if prefix == "users/7/sticker_uploads/":
                    return [{"name": "wave.gif", "url": sticker_url}]
                return []

            with (
                patch.object(media, "get_current_user_id", return_value=7),
                patch.object(media, "_get_stickers_favorites_file", return_value=str(favorites_file)),
                patch.object(media, "_get_stickers_upload_dir", return_value=str(tmp_path / "uploads")),
                patch.object(media, "get_cos_list", side_effect=fake_get_cos_list),
            ):
                app = Flask(__name__)
                with app.test_request_context(json={"path": "user:wave.gif"}):
                    response = media.api_stickers_favorites_add()
                    self.assertEqual(response.status_code, 200)

                with app.test_request_context():
                    response = media.api_stickers_favorites_get()
                    self.assertEqual(response.get_json(), [{
                        "name": "wave",
                        "pack_name": "个人上传",
                        "path": "user:wave.gif",
                        "url": sticker_url,
                    }])
