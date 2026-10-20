import re
from dataclasses import dataclass

GITHUB_PREFIX = "https://github.com/"
OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}")
MAX_URL_LENGTH = 300


class InvalidRepoUrlError(ValueError):
    pass


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str
    clone_url: str


def parse_github_repo_url(url: str) -> RepoRef:
    if not isinstance(url, str) or not url:
        raise InvalidRepoUrlError("Enter a GitHub repository URL like https://github.com/owner/repo")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidRepoUrlError("The URL is too long")
    if any(character.isspace() or not character.isprintable() for character in url):
        raise InvalidRepoUrlError("The URL must not contain whitespace or control characters")
    if not url.isascii():
        raise InvalidRepoUrlError("The URL must contain only ASCII characters")
    if not url.lower().startswith(GITHUB_PREFIX):
        raise InvalidRepoUrlError("Only https://github.com/owner/repo URLs are accepted")
    if "?" in url or "#" in url:
        raise InvalidRepoUrlError("The URL must not contain a query string or fragment")

    path = url[len(GITHUB_PREFIX):]
    if path.endswith("/"):
        path = path[:-1]
    segments = path.split("/")
    if len(segments) != 2:
        raise InvalidRepoUrlError("The URL must have exactly an owner and a repository name")

    owner, repo = segments
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]
    if not OWNER_PATTERN.fullmatch(owner):
        raise InvalidRepoUrlError("The repository owner name is not valid")
    if not REPO_PATTERN.fullmatch(repo) or repo in {".", ".."} or repo.startswith("-"):
        raise InvalidRepoUrlError("The repository name is not valid")

    return RepoRef(owner=owner, repo=repo, clone_url=f"{GITHUB_PREFIX}{owner}/{repo}.git")
