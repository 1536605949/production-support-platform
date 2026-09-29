"""One version, stated in six places, agreed in all six.

Before v2.6 the version was wrong in four different ways at once: the repo root
``VERSION`` file said ``1.7.1-rc1`` (a leftover from the Dify era), the gateway
reported ``2.0.0`` from ``/api/readyz``, both published OpenAPI documents said
``1.7.1``, and the changelog was at ``2.5.0``. A version that is wrong in four
different ways is not a version — it is four guesses, and every one of them is
somebody's answer to "which build is deployed?".

These tests make the ``VERSION`` file the single source of truth and fail the
moment any other surface drifts away from it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: ``(label, path, regex)``. Each regex must capture exactly one group: the
#: version as written in that file. The label is what a failure names, so it has
#: to say *where* the drift is, not just that there is one.
SURFACES = [
    ('gateway settings.version (returned by /api/readyz)',
     'gateway/app/config.py',
     r"version:\s*str\s*=\s*'([^']+)'"),
    ('business service SERVICE_VERSION',
     'business_service/app/config.py',
     r"SERVICE_VERSION\s*=\s*'([^']+)'"),
    ('published agent tool contract',
     'tools/business_api.openapi.yaml',
     r"^  version:\s*(\S+)\s*$"),
    ('published reviewer contract',
     'tools/reviewer_api.openapi.yaml',
     r"^  version:\s*(\S+)\s*$"),
]


def _code_only(relpath: str) -> str:
    """File contents with comment lines removed.

    A guard that asserts "this string appears in this file" is satisfied by a
    comment that contains the same string — which is exactly how the migration
    tooling guard was once defeated in this repo. Every match below therefore
    runs against code, never against prose about code.
    """
    lines = []
    for line in (ROOT / relpath).read_text(encoding='utf-8').splitlines():
        if line.strip().startswith(('#', '//')):
            continue
        lines.append(line)
    return '\n'.join(lines)


def _declared() -> dict[str, str]:
    """Every declared version, keyed by label. Fails if a surface went missing."""
    found: dict[str, str] = {}
    for label, relpath, pattern in SURFACES:
        matches = re.findall(pattern, _code_only(relpath), re.M)
        assert len(matches) == 1, (
            f'{label}: expected exactly one version declaration in {relpath}, '
            f'found {len(matches)}: {matches}'
        )
        found[label] = matches[0]
    return found


def _changelog_top() -> str:
    """The newest heading in the changelog.

    Not comment-stripped: the ``##`` *is* the heading.
    """
    text = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    match = re.search(r'^## (\S+)', text, re.M)
    assert match, 'CHANGELOG.md has no "## <version>" heading'
    return match.group(1)


def test_the_version_file_is_the_source_of_truth():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    assert re.fullmatch(r'\d+\.\d+\.\d+', version), (
        f'VERSION must be plain semver MAJOR.MINOR.PATCH, got {version!r}'
    )


def test_the_surface_table_is_not_silently_empty():
    """A guard over an empty table guards nothing.

    If somebody deletes a surface while "simplifying", the remaining assertions
    would keep passing over a shrinking list.
    """
    assert len(SURFACES) >= 4, f'expected at least 4 version surfaces, got {len(SURFACES)}'
    files = {relpath for _, relpath, _ in SURFACES}
    assert len(files) == len(SURFACES), 'two surfaces point at the same file'


def test_every_declared_surface_matches_the_version_file():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    declared = _declared()
    drift = {label: value for label, value in declared.items() if value != version}
    assert not drift, (
        f'VERSION says {version}, but these surfaces disagree: '
        + '; '.join(f'{label} = {value}' for label, value in drift.items())
    )


def test_the_changelog_documents_the_shipping_version():
    """The changelog's newest entry must be the version being shipped.

    A release whose changelog stops one version short is how "what changed in
    this deploy?" becomes unanswerable at exactly the moment it is asked.
    """
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    assert _changelog_top() == version, (
        f'VERSION is {version} but the newest CHANGELOG heading is {_changelog_top()}'
    )


def test_the_business_service_does_not_hardcode_its_version_again():
    """The service must report ``SERVICE_VERSION``, not a second literal.

    This is the shape the bug actually had: ``main.py`` stated ``'1.7.1'`` twice
    — once for the FastAPI app and once in the ``/healthz`` body. Two literals
    for one fact means the next edit updates one of them.
    """
    source = _code_only('business_service/app/main.py')
    literals = re.findall(r"(?:version\s*=\s*|'version'\s*:\s*)'([^']+)'", source)
    assert not literals, (
        'business_service/app/main.py must use SERVICE_VERSION instead of a '
        f'version literal, found: {literals}'
    )
    assert source.count('SERVICE_VERSION') >= 2, (
        'expected SERVICE_VERSION to be used for both the FastAPI app and /healthz'
    )


@pytest.mark.parametrize('relpath', ['tools/business_api.openapi.yaml',
                                     'tools/reviewer_api.openapi.yaml'])
def test_published_contracts_stay_parseable(relpath):
    """The version edit must not break the YAML.

    A one-line change to ``info.version`` is the kind of edit nobody re-parses
    before committing.
    """
    yaml = pytest.importorskip('yaml')
    document = yaml.safe_load((ROOT / relpath).read_text(encoding='utf-8'))
    assert re.fullmatch(r'\d+\.\d+\.\d+', document['info']['version'])
