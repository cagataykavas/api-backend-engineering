from auth import create_access_token, hash_password, verify_access_token, verify_password


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded != "correct horse battery staple"
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_access_token_roundtrip() -> None:
    token = create_access_token("user-42", secret="test-secret", expires_seconds=60)
    payload = verify_access_token(token, secret="test-secret")
    assert payload["sub"] == "user-42"


def test_access_token_rejects_wrong_secret() -> None:
    token = create_access_token("user-42", secret="test-secret", expires_seconds=60)
    try:
        verify_access_token(token, secret="other-secret")
    except ValueError:
        pass
    else:
        raise AssertionError("token signed with another secret must be rejected")
