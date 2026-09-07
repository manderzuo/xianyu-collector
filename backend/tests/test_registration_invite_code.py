import unittest

from backend.app.services.registration_invite_code import decrypt_invite_code, encrypt_invite_code


class RegistrationInviteCodeTest(unittest.TestCase):
    def test_encrypt_round_trip_preserves_formatted_code(self):
        encrypted = encrypt_invite_code("abcd efgh-ijkl-mnop")
        self.assertNotEqual(encrypted, "ABCD-EFGH-IJKL-MNOP")
        self.assertEqual(decrypt_invite_code(encrypted), "ABCD-EFGH-IJKL-MNOP")

    def test_empty_or_invalid_ciphertext_is_unavailable(self):
        self.assertIsNone(decrypt_invite_code(None))
        self.assertIsNone(decrypt_invite_code("not-a-valid-token"))


if __name__ == "__main__":
    unittest.main()
