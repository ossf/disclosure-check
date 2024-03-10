import logging
import os
import re
from functools import lru_cache
from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool

import github
import requests
from github import Github
from packageurl import PackageURL

from disclosurecheck.collectors.securityinsights import analyze_securityinsights
from disclosurecheck.util.context import Context
from disclosurecheck.util.normalize import normalize_packageurl
from disclosurecheck.util.searchers import find_contacts

from ..util.normalize import normalize_packageurl, sanitize_github_url

logger = logging.getLogger(__name__)

NUGET_WEBSITE = "https://www.nuget.org"

MAX_PULLS_TO_CHECK = 20

COMMON_SECURITY_MD_PATHS = set(
    [
        ".github/security.adoc",
        ".github/security.markdown",
        ".github/security.rst",
        ".github/security.md",
        ".github/SECURITY.md",
        "doc/security.rst",
        "doc/security.md",
        "docs/security.adoc",
        "docs/security.markdown",
        "docs/security.md",
        "docs/security.rst",
        "security.adoc",
        "security.markdown",
        "security.rst",
        "security.md",
        "Security.md",
        "SECURITY.md"
    ]
)
COMMON_OTHER_FILE_PATHS = set(["%name%.gemspec", "Cargo.toml", "LICENSE", "composer.json"])

MAX_CONTENT_SEARCH_FILES = 30


@lru_cache
def get_github_token():
    """Initialize the GitHub token and check to see if we're near our rate limit."""
    token_value = os.environ.get("GITHUB_TOKEN")
    if not token_value:
        logger.error("You do not have a GITHUB_TOKEN defined. Functionality will be limited.")
        return None

    gh = Github(token_value)
    rate_limit = gh.get_rate_limit()
    logger.debug(
        "GitHub Rate Limits: core remaining=%d, search remaining=%d [%s]",
        rate_limit.core.remaining,
        rate_limit.search.remaining,
        rate_limit,
    )
    if rate_limit.search.remaining == 0 or rate_limit.core.remaining == 0:
        logger.error("You have exceeded your GitHub API rate limit. Functionality will be limited.")
        return None

    return token_value


@lru_cache
def analyze(purl: PackageURL, context: Context) -> None:
    """Identifies available disclosure mechanisms for a GitHub repository."""
    logger.debug("Checking GitHub repository: %s", purl)
    if not purl:
        logging.debug("Invalid PackageURL")
        return

    github_token = get_github_token()
    if not github_token:
        logger.warning("Unable to analyze GitHub repository without a GITHUB_TOKEN.")
        return

    gh = Github(github_token)

    context.related_purls.append(normalize_packageurl(purl))

    try:
        repo_obj = gh.get_repo(f"{purl.namespace}/{purl.name}")
    except:
        logger.warning(f"Unable to access GitHub repository: {purl.namespace}/{purl.name}")
        return

    # We probably don't want to report issues to a forked repository.
    if repo_obj.fork:
        context.notes.append(f"Repository [bold blue]{purl.namespace}/{purl.name}[/bold blue] is a fork.")

    if repo_obj.archived:
        context.notes.append(
            f"Repository [bold blue]{purl.namespace}/{purl.name}[/bold blue] has been archived."
        )
    default_branch = repo_obj.default_branch

    _org = repo_obj.owner.login
    _repo = repo_obj.name
    if _org.lower() != purl.namespace.lower() or _repo.lower() != purl.name.lower():
        context.notes.append(
            f"Repository was moved from [bold blue]{purl.namespace}/{purl.name}[/bold blue] to [bold blue]{_org}/{_repo}[/bold blue]."
        )
        context.related_purls.append(PackageURL(type="github", namespace=_org, name=_repo))
        logger.debug(f"Will resume analysis at {_org}/{_repo}.")
        return

    # Check for an email address of the owner (not typical)
    if repo_obj.owner.email:
        context.add_contact(
            {
                "priority": 25,
                "type": "email",
                "source": f"https://github.com/{_org}",
                "value": repo_obj.owner.email,
            }
        )
        logger.info("Found email address for repository owner: %s", repo_obj.owner.email)

    # Check for private vulnerability reporting (REST API)
    url = f"https://api.github.com/repos/{_org}/{_repo}/private-vulnerability-reporting"
    res = requests.get(url, timeout=30)
    if res.status_code == 200:
        enabled = bool(res.json().get('enabled'))
        if enabled:
            context.add_contact(
                {
                    "priority": 0,
                    "type": "github_pvr",
                    "value": f"https://github.com/{_org}/{_repo}/security/advisories/new",
                    "source": url,
                }
            )
            logger.info("Private vulnerability reporting is enabled (via REST API)")
    else:
        # Fall back to web scraping
        url = f"https://github.com/{_org}/{_repo}/security/advisories"
        res = requests.get(url, timeout=30)
        if "Report a vulnerability" in res.text:
            context.add_contact(
                {
                    "priority": 0,
                    "type": "github_pvr",
                    "value": f"https://github.com/{_org}/{_repo}/security/advisories/new",
                    "source": url,
                }
            )
            logger.info("Private vulnerability reporting is enabled (via web scraping)")

    # Check for a contact in a "security.md" in a well-known place (avoid the API call to code search)
    org_purl = PackageURL(type="github", namespace=purl.namespace, name=".github")
    try:
        org_default_branch = gh.get_repo(f"{org_purl.namespace}/{org_purl.name}").default_branch
        org_repo_exists = True
    except:
        org_default_branch = None
        org_repo_exists = False

    _args = []
    for filename in COMMON_SECURITY_MD_PATHS:
        if "%name%" in filename:
            filename = filename.replace("%name%", purl.name)
        _args.append((purl, filename, context, default_branch, 10))
        if org_repo_exists:
            _args.append((org_purl, filename, context, org_default_branch, 10))
    r = ThreadPool(cpu_count() * 2).imap_unordered(_check_github_security_md, _args)

    args = []
    for filename in COMMON_OTHER_FILE_PATHS:
        if "%name%" in filename:
            filename = filename.replace("%name%", purl.name)
        _args.append((purl, filename, context, default_branch, 35))
        if org_repo_exists:
            _args.append((org_purl, filename, context, org_default_branch, 35))
    r = ThreadPool(cpu_count() * 2).imap_unordered(_check_github_security_md, _args)

    # See if the repo supports Security Insights
    analyze_securityinsights(purl, context)

    # Try searching for security.md files and related
    logger.debug("Executing GitHub code search to find SECURITY.md or similar files.")
    files = gh.search_code(f"repo:{_org}/{_repo} path:/(^|\\/)(readme|security)\\.(md|rst|txt)?$/")

    logger.debug("Found %d files", files.totalCount)

    num_files_left = MAX_CONTENT_SEARCH_FILES
    for file in files:
        num_files_left -= 1
        if num_files_left == 0:
            break
        logger.debug("Searching content of [%s]", file.name)
        content = file.decoded_content.decode("utf-8")
        matches = set(re.findall(r"[\w.+-]+(@|\[at\])[\w-]+\.[\w.-]+", content))
        for match in matches:
            context.add_contact(
                {
                    "priority": 25,
                    "type": "email",
                    "source": file.url,
                    "value": match,
                }
            )
    if num_files_left == MAX_CONTENT_SEARCH_FILES:
        logger.debug("No results found in GitHub code search.")

    # Check for recent pull requests
    merged_by_people = set()
    pulls_checked = 0
    pulls = repo_obj.get_pulls(state="closed", sort="updated", direction="desc")
    for pull in pulls:
        if pulls_checked > MAX_PULLS_TO_CHECK:
            break
        pulls_checked += 1

        if pull.merged_by is not None:
            login = pull.merged_by.login
            if login not in merged_by_people:
                merged_by_people.add(login)

    for login in merged_by_people:
        email = gh.get_user(login).email
        if email:
            context.add_contact(
                {
                    "priority": 65,
                    "type": "email",
                    "source": f"Pull requests merged by {login}",
                    "name": login,
                    "value": email,
                }
            )


def _check_github_security_md(args):
    """Checks a "SECURITY.md" file for a security contact.
    Called by thread pool, not intended for external usage, use check_github_security_md() instead."""
    if not args:
        return None

    purl = args[0]
    filename = args[1]
    context = args[2]
    default_branch = args[3] if len(args) > 3 else "master"
    priority = args[4] if len(args) > 4 else 25

    return check_github_security_md(purl, filename, context, default_branch, priority)


def check_github_security_md(
    purl: PackageURL, filename: str, context: Context, default_branch="master", priority=25
):
    """Checks a "SECURITY.md" file for a security contact."""
    logger.debug("Checking GitHub [%s]: %s", filename, purl)
    if purl is None:
        logger.debug("Invalid PackageURL.")
        return

    if purl.type != "github":
        logger.debug("Sorry, only GitHub is supported.")
        return

    if not purl.namespace:
        logger.warning("Unexpected: GitHub package %s does not have a namespace.", purl)
        return

    url = f"https://raw.githubusercontent.com/{purl.namespace}/{purl.name}/{default_branch}/{filename}"
    res = requests.get(url, timeout=30)
    if res.ok:
        find_contacts(url, res.text, context, priority)
    else:
        logger.warning("Error loading URL [%s]: %s", url, res.status_code)
