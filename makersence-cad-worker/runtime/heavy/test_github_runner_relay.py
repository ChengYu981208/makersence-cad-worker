import base64
import json
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import github_runner_relay as relay


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class RunnerRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = cls.private_key.public_key().public_numbers()
        cls.jwk = {
            "kid": "test-key", "kty": "RSA", "alg": "RS256", "use": "sig",
            "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }

    def setUp(self):
        with relay._lock:
            relay._tasks.clear()

    def token(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": relay.ISSUER, "aud": relay.AUDIENCE,
            "repository": relay.REPOSITORY,
            "ref": "refs/heads/main",
            "workflow_ref": relay.TRUSTED_WORKFLOWS["refs/heads/main"],
            "run_id": "12345678", "run_attempt": "1", "sha": "a" * 40,
            "iat": now, "nbf": now - 1, "exp": now + 300,
        }
        claims.update(overrides)
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "test-key"}).encode())
        body = _b64(json.dumps(claims, separators=(",", ":")).encode())
        signed = (head + "." + body).encode("ascii")
        signature = self.private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        return head + "." + body + "." + _b64(signature)

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token())}

    def request(self):
        return {
            "source_url": "https://makerworld.com/source.3mf",
            "counterpart_url": "https://makerworld.com/counterpart.3mf",
            "expected_instances": 2,
        }

    def test_oidc_claims_are_verified_and_bound_to_workflow(self):
        with patch.object(relay, "_get_jwks", return_value=[self.jwk]):
            claims = relay.verify_github_oidc("Bearer " + self.token())
            self.assertEqual(claims["repository"], relay.REPOSITORY)
            with self.assertRaises(relay.RelayError) as caught:
                relay.verify_github_oidc("Bearer " + self.token(ref="refs/heads/untrusted"))
            self.assertEqual(caught.exception.status, 401)

    def test_claim_and_result_are_bound_to_one_run(self):
        task_id = "1" * 32
        relay.enqueue_task(task_id, self.request())
        alignment = {
            "algorithm_version": "counterpart-alignment-v4-exact-mesh-gap-bounded",
            "status": "ALIGNED",
            "source": {"triangle_count": 1200},
        }
        body = {"run_id": "12345678", "run_attempt": 1, "sha": "a" * 40}
        with patch.object(relay, "_get_jwks", return_value=[self.jwk]):
            claimed = relay.claim_task(self.headers(), body)
            self.assertEqual(claimed["task"]["task_id"], task_id)
            self.assertEqual(relay.claim_task(self.headers(), body)["task"], None)
            result = {**body, "task_id": task_id, "status": "completed", "alignment": alignment}
            self.assertTrue(relay.complete_task(self.headers(), result)["ok"])
        saved = relay.get_task(task_id)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["alignment"], alignment)

    def test_claim_rejects_body_identity_mismatch(self):
        relay.enqueue_task("2" * 32, self.request())
        with patch.object(relay, "_get_jwks", return_value=[self.jwk]):
            with self.assertRaises(relay.RelayError) as caught:
                relay.claim_task(self.headers(), {"run_id": "different", "run_attempt": 1, "sha": "a" * 40})
            self.assertEqual(caught.exception.status, 401)

    def test_expired_runner_lease_can_be_claimed_by_another_run(self):
        task_id = "3" * 32
        relay.enqueue_task(task_id, self.request())
        first_body = {"run_id": "12345678", "run_attempt": 1, "sha": "a" * 40}
        first_token = self.token()
        with patch.object(relay, "_get_jwks", return_value=[self.jwk]):
            first = relay.claim_task(self.headers(first_token), first_body)
            self.assertEqual(first["task"]["task_id"], task_id)
            with relay._lock:
                relay._tasks[task_id]["updated_at"] = time.time() - relay.CLAIM_LEASE_SECONDS - 1
            self.assertEqual(relay.get_task(task_id)["status"], "queued")
            second_body = {"run_id": "87654321", "run_attempt": 1, "sha": "b" * 40}
            second_token = self.token(run_id="87654321", sha="b" * 40)
            second = relay.claim_task(self.headers(second_token), second_body)
            self.assertEqual(second["task"]["task_id"], task_id)
            stale_result = {
                **first_body, "task_id": task_id, "status": "failed",
                "failure_code": "WORKER_FAILED",
            }
            with self.assertRaises(relay.RelayError) as caught:
                relay.complete_task(self.headers(first_token), stale_result)
            self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
