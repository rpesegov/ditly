"""Сценарий менеджера: учётка, проект, ссылка, переход, фильтр автоматических заходов."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ditly.clicks import classify_user_agent, country_from_ip, moscow_day, record_click
from ditly.config import Settings, normalize_base_url
from ditly.identity import create_user
from ditly.web import create_app

ROOT = Path(__file__).resolve().parents[1]
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
LONG_URL = "https://vk.com/app123?ref=1"


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "ditly.sqlite"
        self.app = create_app(
            Settings(
                db_path=self.db_path,
                base_url="http://short.test",
                secret_key="test-secret",
                geoip_db=None,
                trust_proxy=False,
                host="127.0.0.1",
                port=8000,
            )
        )
        self.client = self.app.test_client()
        create_user("anna", "password123")
        self.login("anna", "password123")

    def csrf(self) -> str:
        with self.client.session_transaction() as sess:
            token = sess.get("csrf_token")
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        return token

    def login(self, login: str, password: str):
        self.client.get("/login")
        response = self.client.post(
            "/login",
            data={"login": login, "password": password, "csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))

    def logout(self):
        response = self.client.post("/logout", data={"csrf_token": self.csrf()})
        self.assertEqual(response.status_code, 302)

    def create_project(self, name: str = "Клиент ВК") -> str:
        response = self.client.post(
            "/projects/new",
            data={"name": name, "csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return response.headers["Location"].rstrip("/").split("/")[-1]

    def create_link(self, project_id: str, label: str = "Приложение ВК", url: str = LONG_URL) -> str:
        response = self.client.post(
            f"/projects/{project_id}/links/new",
            data={"label": label, "url": url, "csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        page = self.client.get(f"/projects/{project_id}")
        text = page.get_data(as_text=True)
        self.assertNotIn("http://localhost", text)
        found = re.search(r"http://short\.test/([A-Za-z0-9]{4,16})", text)
        self.assertIsNotNone(found, text)
        return found.group(1)

    def link_id(self, code: str) -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT id FROM links WHERE code = ?", (code,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        return row[0]

    def test_click_is_recorded_and_bot_can_be_filtered(self):
        project_id = self.create_project()
        code = self.create_link(project_id)
        link_id = self.link_id(code)

        human = self.client.get(
            f"/{code}",
            headers={"User-Agent": IPHONE, "Referer": "https://vk.com/wall-1"},
        )
        self.assertEqual(human.status_code, 302)
        self.assertEqual(human.headers["Location"], LONG_URL)
        self.assertIn("ditly_visitor", human.headers.get("Set-Cookie", ""))

        bot = self.client.get(f"/{code}", headers={"User-Agent": GOOGLEBOT})
        self.assertEqual(bot.status_code, 302)
        self.assertEqual(bot.headers["Location"], LONG_URL)

        conn = sqlite3.connect(self.db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(clicks)")}
            rows = conn.execute(
                "SELECT is_bot, referrer, device, country, visitor_key FROM clicks ORDER BY is_bot"
            ).fetchall()
        finally:
            conn.close()
        self.assertNotIn("ip", columns)
        self.assertNotIn("user_agent", columns)
        self.assertEqual(len(rows), 2)
        human_row, bot_row = rows
        self.assertEqual(human_row[0], 0)
        self.assertEqual(human_row[1], "https://vk.com/wall-1")
        self.assertEqual(human_row[2], "phone")
        self.assertIsNone(human_row[3])
        self.assertTrue(human_row[4])
        self.assertEqual(bot_row[0], 1)

        page = self.client.get(f"/projects/{project_id}")
        self.assertIn('data-clicks="1"', page.get_data(as_text=True))

        hidden = self.client.get(f"/projects/{project_id}/links/{link_id}")
        hidden_html = hidden.get_data(as_text=True)
        self.assertEqual(hidden.status_code, 200)
        self.assertIn('data-total="1"', hidden_html)
        self.assertIn("Автоматические заходы скрыты", hidden_html)
        self.assertIn('data-hidden="1"', hidden_html)
        self.assertIn("Телефон", hidden_html)
        self.assertIn("https://vk.com/wall-1", hidden_html)
        self.assertIn("Разных посетителей: 1", hidden_html)

        shown = self.client.get(f"/projects/{project_id}/links/{link_id}?bots=1")
        shown_html = shown.get_data(as_text=True)
        self.assertIn('data-total="2"', shown_html)
        self.assertIn("Автоматические заходы показаны", shown_html)

    def test_disabled_link_does_not_redirect_or_count(self):
        project_id = self.create_project()
        code = self.create_link(project_id)
        link_id = self.link_id(code)
        self.client.get(f"/{code}", headers={"User-Agent": CHROME})

        update = self.client.post(
            f"/projects/{project_id}/links/{link_id}/edit",
            data={
                "csrf_token": self.csrf(),
                "label": "Приложение ВК",
                "url": "https://vk.com/app123?ref=2",
            },
        )
        self.assertEqual(update.status_code, 302)

        missed = self.client.get(f"/{code}", headers={"User-Agent": CHROME})
        self.assertEqual(missed.status_code, 404)
        self.assertIn("выключена", missed.get_data(as_text=True))

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

        again = self.client.post(
            f"/projects/{project_id}/links/{link_id}/edit",
            data={
                "csrf_token": self.csrf(),
                "label": "Новая пометка",
                "url": "https://example.com/new",
                "enabled": "1",
            },
        )
        self.assertEqual(again.status_code, 302)
        followed = self.client.get(f"/{code}", headers={"User-Agent": CHROME})
        self.assertEqual(followed.status_code, 302)
        self.assertEqual(followed.headers["Location"], "https://example.com/new")

    def test_moscow_day_boundary_on_stats_page(self):
        project_id = self.create_project()
        code = self.create_link(project_id)
        link_id = self.link_id(code)
        record_click(
            link_id,
            is_bot=False,
            referrer=None,
            device="computer",
            country=None,
            visitor_key="visitor-key-one-aaaaaa",
            clicked_at="2026-10-27T20:30:00+00:00",
        )
        record_click(
            link_id,
            is_bot=False,
            referrer=None,
            device="computer",
            country=None,
            visitor_key="visitor-key-two-bbbbbb",
            clicked_at="2026-10-27T21:30:00+00:00",
        )
        page = self.client.get(f"/projects/{project_id}/links/{link_id}")
        html = page.get_data(as_text=True)
        self.assertIn("27.10.2026", html)
        self.assertIn("28.10.2026", html)
        self.assertIn('data-total="2"', html)

    def test_stranger_does_not_see_project_until_it_is_shared(self):
        project_id = self.create_project("Секретный клиент")
        code = self.create_link(project_id)
        link_id = self.link_id(code)
        create_user("boris", "password123")

        self.logout()
        self.login("boris", "password123")
        listing = self.client.get("/projects")
        self.assertNotIn("Секретный клиент", listing.get_data(as_text=True))
        hidden = self.client.get(f"/projects/{project_id}")
        self.assertEqual(hidden.status_code, 404)
        self.assertNotIn("Секретный клиент", hidden.get_data(as_text=True))
        stats = self.client.get(f"/projects/{project_id}/links/{link_id}")
        self.assertEqual(stats.status_code, 404)

        self.logout()
        self.login("anna", "password123")
        shared = self.client.post(
            f"/projects/{project_id}/members",
            data={"csrf_token": self.csrf(), "login": "boris", "role": "view"},
        )
        self.assertEqual(shared.status_code, 302)

        self.logout()
        self.login("boris", "password123")
        visible = self.client.get(f"/projects/{project_id}")
        body = visible.get_data(as_text=True)
        self.assertEqual(visible.status_code, 200)
        self.assertIn(LONG_URL, body)
        self.assertNotIn("Новая ссылка", body)
        self.assertNotIn('id="access"', body)
        forbidden = self.client.post(
            f"/projects/{project_id}/links/new",
            data={
                "csrf_token": self.csrf(),
                "label": "Чужая",
                "url": "https://example.com",
            },
        )
        self.assertEqual(forbidden.status_code, 403)

        self.logout()
        self.login("anna", "password123")
        self.client.post(
            f"/projects/{project_id}/members",
            data={"csrf_token": self.csrf(), "login": "boris", "role": "edit"},
        )
        self.logout()
        self.login("boris", "password123")
        edited = self.client.post(
            f"/projects/{project_id}/links/new",
            data={
                "csrf_token": self.csrf(),
                "label": "Ещё одна",
                "url": "https://example.com/second",
            },
        )
        self.assertEqual(edited.status_code, 302)
        cannot_share = self.client.post(
            f"/projects/{project_id}/members",
            data={"csrf_token": self.csrf(), "login": "anna", "role": "view"},
        )
        self.assertEqual(cannot_share.status_code, 403)

    def test_new_account_starts_without_projects_and_last_manager_stays(self):
        project_id = self.create_project()
        created = self.client.post(
            "/users/new",
            data={
                "csrf_token": self.csrf(),
                "login": "vera",
                "password": "password123",
                "password_again": "password123",
            },
        )
        self.assertEqual(created.status_code, 302)
        self.logout()
        self.login("vera", "password123")
        page = self.client.get("/projects")
        self.assertIn("Проектов пока нет", page.get_data(as_text=True))

        self.logout()
        self.login("anna", "password123")
        conn = sqlite3.connect(self.db_path)
        try:
            anna_id = conn.execute(
                "SELECT id FROM users WHERE login = 'anna'"
            ).fetchone()[0]
        finally:
            conn.close()
        removed = self.client.post(
            f"/projects/{project_id}/members/remove",
            data={"csrf_token": self.csrf(), "user_id": anna_id},
        )
        self.assertEqual(removed.status_code, 302)
        still = self.client.get(f"/projects/{project_id}")
        self.assertEqual(still.status_code, 200)
        self.assertIn("должен остаться", still.get_data(as_text=True))

    def test_anonymous_short_link_works_and_there_is_no_open_signup(self):
        self.logout()
        guest = self.app.test_client()
        self.assertEqual(guest.get("/projects").status_code, 302)
        self.assertIn("/login", guest.get("/projects").headers["Location"])
        missing = guest.get("/no-such-page")
        self.assertEqual(missing.status_code, 404)
        self.assertNotIn("Зарегистрир", missing.get_data(as_text=True))

        self.login("anna", "password123")
        project_id = self.create_project()
        code = self.create_link(project_id)
        self.logout()
        followed = guest.get(f"/{code}", headers={"User-Agent": CHROME})
        self.assertEqual(followed.status_code, 302)
        self.assertEqual(followed.headers["Location"], LONG_URL)


class RuleTest(unittest.TestCase):
    def test_device_and_bot_flags(self):
        bot, device = classify_user_agent(None)
        self.assertFalse(bot)
        self.assertIsNone(device)
        bot, device = classify_user_agent(IPHONE)
        self.assertFalse(bot)
        self.assertEqual(device, "phone")
        bot, device = classify_user_agent(CHROME)
        self.assertFalse(bot)
        self.assertEqual(device, "computer")
        bot, device = classify_user_agent(
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
        )
        self.assertFalse(bot)
        self.assertEqual(device, "tablet")
        bot, _device = classify_user_agent(GOOGLEBOT)
        self.assertTrue(bot)
        bot, _device = classify_user_agent("curl/8.4.0")
        self.assertTrue(bot)

    def test_moscow_midnight(self):
        self.assertEqual(moscow_day("2026-10-27T20:30:00+00:00"), "2026-10-27")
        self.assertEqual(moscow_day("2026-10-27T21:30:00+00:00"), "2026-10-28")

    def test_country_is_empty_without_a_database_and_for_local_ip(self):
        self.assertIsNone(country_from_ip("127.0.0.1", None))
        self.assertIsNone(country_from_ip("8.8.8.8", None))
        self.assertIsNone(country_from_ip("8.8.8.8", "/no/such/file.mmdb"))

    def test_base_url_is_a_setting_without_a_path(self):
        self.assertEqual(normalize_base_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        self.assertEqual(normalize_base_url("https://go.example.com"), "https://go.example.com")
        with self.assertRaises(SystemExit):
            normalize_base_url("http://127.0.0.1:8000/r")
        with self.assertRaises(SystemExit):
            normalize_base_url("ditly.internal")

    def test_password_check_stays_behind_the_identity_boundary(self):
        package = ROOT / "ditly"
        for path in package.rglob("*.py"):
            if path.name == "identity.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("password_hash", text, path.name)
            self.assertNotIn("pbkdf2", text, path.name)
            self.assertNotIn("SESSION_USER", text, path.name)
            self.assertNotIn('session["user_id"]', text, path.name)
            self.assertNotIn("session['user_id']", text, path.name)


class CliTest(unittest.TestCase):
    def test_create_user_command(self):
        with TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["DITLY_DATA_DIR"] = tmp
            env["DITLY_BASE_URL"] = "http://127.0.0.1:8765"
            env["DITLY_PASSWORD"] = "secretpass"
            env.pop("DITLY_SECRET", None)
            env.pop("DITLY_DB", None)
            completed = subprocess.run(
                [sys.executable, "-m", "ditly", "create-user", "ivan"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("создана", completed.stdout)
            conn = sqlite3.connect(Path(tmp) / "ditly.sqlite")
            try:
                row = conn.execute("SELECT login, password_hash FROM users").fetchone()
            finally:
                conn.close()
            self.assertEqual(row[0], "ivan")
            self.assertNotIn("secretpass", row[1])
            self.assertTrue(row[1].startswith("pbkdf2_sha256$"))


if __name__ == "__main__":
    unittest.main()
