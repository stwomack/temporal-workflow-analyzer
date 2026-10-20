import pytest

from app.repo_url import InvalidRepoUrlError, RepoRef, parse_github_repo_url


@pytest.mark.parametrize(
    ("url", "owner", "repo"),
    [
        ("https://github.com/temporalio/samples-python", "temporalio", "samples-python"),
        ("https://github.com/temporalio/samples-python/", "temporalio", "samples-python"),
        ("https://github.com/temporalio/samples-python.git", "temporalio", "samples-python"),
        ("https://github.com/temporalio/samples-python.git/", "temporalio", "samples-python"),
        ("https://GitHub.com/Temporalio/Sdk_Go", "Temporalio", "Sdk_Go"),
        ("https://github.com/a/b", "a", "b"),
        ("https://github.com/my-org/repo.name", "my-org", "repo.name"),
        ("https://github.com/user123/.github", "user123", ".github"),
        ("https://github.com/x/_private-ish", "x", "_private-ish"),
    ],
)
def test_accepted_urls(url, owner, repo):
    assert parse_github_repo_url(url) == RepoRef(
        owner=owner, repo=repo, clone_url=f"https://github.com/{owner}/{repo}.git"
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "github.com/temporalio/sdk-python",
        "http://github.com/temporalio/sdk-python",
        "git@github.com:temporalio/sdk-python.git",
        "ssh://git@github.com/temporalio/sdk-python",
        "file:///etc/passwd",
        "https://gitlab.com/temporalio/sdk-python",
        "https://github.com.evil.com/temporalio/sdk-python",
        "https://evil.com/https://github.com/a/b",
        "https://user:pass@github.com/temporalio/sdk-python",
        "https://token@github.com/temporalio/sdk-python",
        "https://github.com:443/temporalio/sdk-python",
        "https://github.com/temporalio/sdk-python?ref=main",
        "https://github.com/temporalio/sdk-python#readme",
        " https://github.com/temporalio/sdk-python",
        "https://github.com/temporalio/sdk-python ",
        "https://github.com/temporalio/sdk python",
        "https://github.com/temporalio/sdk-python\n--upload-pack=touch /tmp/pwned",
        "https://github.com/-oupload-pack=touch/repo",
        "https://github.com/owner/--upload-pack=evil",
        "https://github.com/owner/-repo",
        "https://github.com/temporalio",
        "https://github.com/temporalio/",
        "https://github.com/temporalio/sdk-python/tree/main",
        "https://github.com/temporalio/sdk-python/issues",
        "https://github.com//sdk-python",
        "https://github.com/temporalio/..",
        "https://github.com/../etc",
        "https://github.com/temporalio/sdk-python;rm -rf /",
        "https://github.com/temporalio/$(whoami)",
        "https://github.com/temporalio/`id`",
        "https://github.com/temporalio/repo|cat",
        "https://github.com/temporalio/repo&&ls",
        "https://github.com/owner-/repo",
        "https://github.com/ow--ner/repo",
        "https://github.com/temporalio/%2e%2e",
        "https://github.com/temporalio/répo",
        "https://github.com/temporalio/.git",
        "https://github.com/" + "a" * 40 + "/repo",
        "https://github.com/owner/" + "r" * 101,
    ],
)
def test_rejected_urls(url):
    with pytest.raises(InvalidRepoUrlError):
        parse_github_repo_url(url)


def test_non_string_is_rejected():
    with pytest.raises(InvalidRepoUrlError):
        parse_github_repo_url(None)


def test_error_is_a_value_error_with_message():
    with pytest.raises(ValueError, match="github.com"):
        parse_github_repo_url("https://gitlab.com/a/b")
