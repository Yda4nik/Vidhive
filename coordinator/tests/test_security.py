from app.services.security import hash_password, verify_password


def test_hash_verify_roundtrip():
    h = hash_password("s3cret")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False


def test_salt_makes_hashes_differ():
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_garbage():
    assert verify_password("x", "not-a-valid-hash") is False
