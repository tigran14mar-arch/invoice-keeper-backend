"""Границы доступа к упрощённой тестовой привязке."""
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import bot


class TestLink(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.is_chat_admin.return_value = True
        self.message = {"message": {"text": "/link", "chat": {"id": -123, "type": "group", "title": "Test"}, "from": {"id": 42}}}
        self.config = SimpleNamespace(telegram_test_link_enabled=True, admin_username="admin")
        self.patches = [patch.object(bot, "settings", self.config),
                        patch.object(bot.services, "user_by_chat", return_value=None),
                        patch.object(bot, "query_one", return_value={"id": 1, "telegram_chat_id": None}),
                        patch.object(bot.services, "create_link_code", return_value={"code": "test-code"}),
                        patch.object(bot, "_link_group", return_value="linked")]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def run_command(self):
        return bot.handle_update(self.client, self.message, date(2026, 10, 25))

    def test_disabled(self):
        self.config.telegram_test_link_enabled = False
        self.assertEqual(self.run_command(), "test_link_disabled")
        self.mocks[3].assert_not_called()

    def test_non_admin(self):
        self.client.is_chat_admin.return_value = False
        self.assertEqual(self.run_command(), "forbidden")
        self.mocks[3].assert_not_called()

    def test_already_linked(self):
        self.mocks[1].return_value = {"id": 1}
        self.assertEqual(self.run_command(), "already_linked")
        self.mocks[3].assert_not_called()

    def test_does_not_replace_other_group(self):
        self.mocks[2].return_value = {"id": 1, "telegram_chat_id": "-456"}
        self.assertEqual(self.run_command(), "other_group_linked")
        self.mocks[3].assert_not_called()

    def test_missing_app_admin(self):
        self.mocks[2].return_value = None
        self.assertEqual(self.run_command(), "admin_not_found")

    def test_admin_links(self):
        self.assertEqual(self.run_command(), "linked")
        self.client.is_chat_admin.assert_called_once_with("-123", 42)
        self.mocks[3].assert_called_once_with(1)
        self.mocks[4].assert_called_once_with(self.client, "-123", "Test", "test-code", {"id": 42})

    def test_private_chat_explains(self):
        self.message["message"]["chat"]["type"] = "private"
        self.assertEqual(self.run_command(), "link_in_group")
        self.mocks[3].assert_not_called()
