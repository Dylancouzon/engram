from engram.redact import redact


def test_clean_text_passes_through():
    r = redact("Dylan prefers uv over pip for Python projects")
    assert r.clean and r.text == "Dylan prefers uv over pip for Python projects"


def test_aws_key_redacted():
    r = redact("my key is AKIAIOSFODNN7EXAMPLE ok")
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert "aws-access-key" in r.hits
    assert "ok" in r.text  # surrounding context survives


def test_github_token_redacted():
    r = redact("token ghp_" + "a1B2" * 10)
    assert "github-token" in r.hits


def test_openai_style_key_redacted():
    r = redact("OPENAI sk-proj-abc123DEF456ghi789JKL012")
    assert "api-key" in r.hits


def test_jwt_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P"
    r = redact(f"bearer {jwt}")
    assert "jwt" in r.hits and jwt not in r.text


def test_url_password_redacted_keeps_url():
    r = redact("db is at postgres://admin:s3cretPW@db.host:5432/prod")
    assert "s3cretPW" not in r.text
    assert "admin" in r.text and "db.host" in r.text
    assert "url-credential" in r.hits


def test_assigned_password_redacted():
    r = redact("the wifi password: hunter2-secret")
    assert "hunter2-secret" not in r.text
    assert "password" in r.text  # the label survives, the value doesn't


def test_high_entropy_token_redacted():
    r = redact("value is x9K2mQ7pL4nR8vT3wY6zB1cD5fG0hJa noted")
    assert "high-entropy" in r.hits


def test_hex_sha_not_redacted():
    sha = "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"
    r = redact(f"the fix landed in commit {sha}")
    assert sha in r.text and r.clean


def test_private_key_refuses_entirely():
    r = redact("here: -----BEGIN RSA PRIVATE KEY-----\nMIIEow...")
    assert r.refused and r.text == ""


def test_disabled_passes_everything():
    r = redact("AKIAIOSFODNN7EXAMPLE", enabled=False)
    assert r.clean and "AKIA" in r.text
