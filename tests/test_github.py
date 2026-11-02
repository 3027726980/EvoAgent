# 作者：LHF
# 时间：2026-11-01
# 作用：验证 GitHub Webhook 签名。
import hashlib
import hmac
import unittest

from evoagent.github import verify_signature


class GitHubSignatureTests(unittest.TestCase):
    # 验证 GitHub 签名校验。
    def test_signature_verification(self):
        body = b'{"ok":true}'
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("secret", body, signature))
        self.assertFalse(verify_signature("wrong", body, signature))


if __name__ == "__main__":
    unittest.main()

