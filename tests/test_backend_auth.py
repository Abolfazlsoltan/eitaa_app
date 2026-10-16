import unittest
from unittest.mock import patch

from backend import auth


class AuthHardeningTests(unittest.TestCase):
    def test_session_token_round_trip_and_tamper_rejection(self):
        token = auth.make_token(7)
        self.assertEqual(auth.read_token(token), 7)
        self.assertIsNone(auth.read_token(token + "x"))

    def test_invalid_user_ids_are_not_signed(self):
        with self.assertRaises(ValueError):
            auth.make_token(0)

    def test_password_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            auth.hash_pw("short")
        with self.assertRaises(ValueError):
            auth.hash_pw("x" * 1025)

    def test_init_data_rejects_duplicate_fields(self):
        self.assertIsNone(
            auth.validate_init_data("auth_date=1&auth_date=2&hash=" + "0" * 64, "token")
        )

    def test_init_data_rejects_oversized_input(self):
        self.assertIsNone(auth.validate_init_data("x" * 9000, "token"))

    def test_role_permissions_match_expected_workflows(self):
        self.assertTrue(auth.can("owner", "edit"))
        self.assertTrue(auth.can("owner", "publish"))
        self.assertTrue(auth.can("editor", "edit"))
        self.assertFalse(auth.can("editor", "publish"))
        self.assertTrue(auth.can("publisher", "publish"))
        self.assertFalse(auth.can("publisher", "edit"))
        self.assertTrue(auth.can("approver", "approve"))
        self.assertFalse(auth.can("approver", "publish"))
        self.assertTrue(auth.can("analyst", "analytics"))
        self.assertFalse(auth.can("analyst", "edit"))

    def test_unknown_roles_and_permissions_are_denied(self):
        self.assertFalse(auth.can("unknown", "publish"))
        self.assertFalse(auth.can("publisher", "admin"))
        self.assertEqual(auth.role_permissions("unknown"), [])


if __name__ == "__main__":
    unittest.main()
