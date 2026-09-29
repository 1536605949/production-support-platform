# -*- coding: utf-8 -*-
"""Guards for the migration *tooling* (as opposed to the migration tree).

``tests/test_migrations.py`` checks the migration history. This file checks the
two scripts that operate on it, because both had a defect that produced no error
message at all:

1. ``scripts/migrate.sh`` ran alembic inside the ``gateway`` container, which has
   the source COPYed in and no bind mount. So ``upgrade head`` resolved "head" to
   whatever the *image* knew about — it applied nothing and still printed 完成,
   leaving ``alembic_version`` at ``0001_baseline`` while a ``0002`` migration sat
   on disk. And ``check`` compared the image's models against the database, so it
   reported "no drift" precisely when a model change was pending.

2. ``scripts/new_migration.sh``'s predecessor wrote the generated file into a
   ``--rm`` container's writable layer, where it was deleted on exit.

Both are fixed by bind-mounting the working copy over ``/app``. These tests assert
the mount is present and derive their expectations from the tree rather than
hardcoding them, so adding a migration does not break them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SH = ROOT / 'scripts' / 'migrate.sh'
NEW_MIGRATION_SH = ROOT / 'scripts' / 'new_migration.sh'
MAKEFILE = ROOT / 'Makefile'
VERSIONS = ROOT / 'migrations' / 'versions'


def _bash() -> str:
    """Path to a usable bash, or skip.

    The tooling is bash, so testing it means running bash. On a machine without
    one (a bare Windows Python) there is nothing to assert — skipping is honest,
    failing would be noise.
    """
    found = shutil.which('bash')
    if not found:
        pytest.skip('没有 bash，跳过迁移工具链测试')
    return found


def _run(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    # A path relative to the repo, with the repo as cwd: an absolute Windows path
    # handed straight to bash is a path-conversion coin flip under MSYS.
    return subprocess.run(
        [_bash(), script.relative_to(ROOT).as_posix(), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )


def _max_ordinal() -> int:
    ordinals = [
        int(m.group(1))
        for path in VERSIONS.glob('*.py')
        if (m := re.match(r'^(\d{4})_', path.name))
    ]
    assert ordinals, '迁移目录里一个 000N_ 文件都没有'
    return max(ordinals)


def _code_only(src: str) -> str:
    """Source with comment lines removed.

    Needed, not cosmetic. The first version of the mount guard asserted the
    literal ``-v "$ROOT:/app"`` was present in ``migrate.sh`` — and it stayed
    green after the mount was deleted from the command, because the comment that
    *explains* the mount contains the same text. A guard a comment can satisfy
    guards nothing.
    """
    return '\n'.join(
        line for line in src.splitlines() if not line.lstrip().startswith('#')
    )


def _alembic_fn_code() -> str:
    """The body of ``alembic()`` in migrate.sh, comments stripped."""
    body = MIGRATE_SH.read_text(encoding='utf-8').split('alembic() {', 1)[1]
    return _code_only(body.split('\n}', 1)[0])


# --------------------------------------------------------------------------
# the mount is what makes both scripts describe the real source
# --------------------------------------------------------------------------

def test_migrate_sh_mounts_the_working_copy() -> None:
    """Without this mount ``upgrade`` and ``check`` silently describe the image.

    This is the assertion that would have caught the observed incident: the run
    printed 完成 while applying nothing.
    """
    assert '-v "$ROOT:/app"' in _alembic_fn_code(), (
        'migrate.sh 的 alembic() 没有把工作副本挂到 /app —— '
        '容器会去读镜像里那份旧 migrations/ 和 models.py，'
        'upgrade 会「成功」但不应用任何迁移，check 会在有漂移时报「无漂移」'
    )


def test_migrate_sh_derives_the_mount_source_from_the_script() -> None:
    """The mounted path must be computed, not assumed.

    A hardcoded path would mount the wrong directory (or nothing) whenever the
    checkout lives somewhere else, and mounting nothing is the bug again.
    """
    src = _code_only(MIGRATE_SH.read_text(encoding='utf-8'))
    assert 'ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"' in src, (
        'ROOT 应该由脚本自身位置推导，否则挂载路径会依赖当前目录'
    )
    assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in src


def test_migrate_sh_runs_alembic_as_the_invoking_user() -> None:
    """The mount writes into the host tree; root-owned files there are a trap."""
    assert '--user "$(id -u):$(id -g)"' in _alembic_fn_code(), (
        'alembic 以 root 运行会在宿主机留下 root 属主的 __pycache__'
    )


# --------------------------------------------------------------------------
# new_migration.sh
# --------------------------------------------------------------------------

def test_make_revision_goes_through_the_script() -> None:
    """``make revision`` must not call alembic directly.

    Calling it directly is how the generated file ends up in a ``--rm``
    container: nothing asserts that it reached the host.
    """
    src = _code_only(MAKEFILE.read_text(encoding='utf-8'))
    target = src.split('\nrevision:', 1)[1].split('\n\n', 1)[0]
    assert 'scripts/new_migration.sh' in target, (
        'make revision 应该调用 scripts/new_migration.sh（它会挂载工作副本并断言文件落盘）'
    )
    assert 'alembic revision' not in target, (
        'make revision 不应该直接调 alembic revision'
    )


def test_new_migration_script_asserts_the_file_landed_on_the_host() -> None:
    """The whole point of the script is this check, not the alembic call."""
    src = _code_only(NEW_MIGRATION_SH.read_text(encoding='utf-8'))
    assert '[ -f "$GENERATED" ]' in src, (
        'new_migration.sh 必须断言迁移文件真的出现在宿主机上'
    )
    assert '--rev-id' in src, 'rev-id 必须显式给出，否则是随机 hex'
    assert '-v "$ROOT:/app"' in src, (
        'new_migration.sh 也必须挂载工作副本，否则 autogenerate 对比的是镜像里的旧模型'
    )


def test_new_migration_script_numbers_the_next_revision() -> None:
    """The ordinal is max(existing) + 1, zero-padded to four digits."""
    expected = f'{_max_ordinal() + 1:04d}_add_refund_audit_column'
    proc = _run(NEW_MIGRATION_SH, 'add refund audit column', '--dry-run')
    assert proc.returncode == 0, proc.stderr
    match = re.search(r'--rev-id (\S+)', proc.stdout)
    assert match, f'dry-run 没有打印 rev-id:\n{proc.stdout}'
    assert match.group(1) == expected, (
        f'编号应该是 {expected}，实际是 {match.group(1)}'
    )


def test_new_migration_script_slugifies_an_explicit_slug() -> None:
    """An explicit slug is normalised, so the filename stays importable."""
    proc = _run(NEW_MIGRATION_SH, 'Add Refund Audit Column', 'Refund Audit', '--dry-run')
    assert proc.returncode == 0, proc.stderr
    assert re.search(r'--rev-id \d{4}_refund_audit\b', proc.stdout), proc.stdout


def test_new_migration_script_rejects_a_hex_revision_id() -> None:
    """A random hex id is what alembic would produce by default.

    ``migrate.sh`` stamps the baseline *by name*, and
    ``test_migrations.py::test_revision_ids_are_human_readable`` rejects hex — so
    the script has to refuse before it writes anything.
    """
    proc = _run(NEW_MIGRATION_SH, 'whatever', '--dry-run', env={'REV_ID': 'deadbeef1234'})
    assert proc.returncode != 0, '随机 hex revision id 应该被拒绝'
    assert '000N_xxx' in proc.stderr, proc.stderr


def test_new_migration_script_refuses_to_overwrite_a_revision() -> None:
    """Silently overwriting a migration would lose schema history."""
    existing = sorted(VERSIONS.glob('*.py'))[0].stem
    proc = _run(NEW_MIGRATION_SH, 'whatever', '--dry-run', env={'REV_ID': existing})
    assert proc.returncode != 0, f'{existing} 已存在，不应被覆盖'
    assert '已存在' in proc.stderr, proc.stderr


def test_new_migration_script_dry_run_shows_the_mount() -> None:
    """The dry-run must show the mount, so the fix is visible without running it."""
    proc = _run(NEW_MIGRATION_SH, 'add refund audit column', '--dry-run')
    assert proc.returncode == 0, proc.stderr
    assert '-v ' in proc.stdout and ':/app' in proc.stdout, proc.stdout


def test_file_template_does_not_write_its_own_extension() -> None:
    """``file_template`` must not end in ``.py``.

    alembic appends the extension to whatever the template expands to, so
    ``%(rev)s.py`` produces ``0002_foo.py.py``. That is not a Python module: the
    next ``alembic upgrade`` cannot import the revision it just generated. Found
    by running the generator against the deployment, not by reading the docs.
    """
    cfg = (ROOT / 'alembic.ini').read_text(encoding='utf-8')
    line = next(
        ln for ln in cfg.splitlines()
        if ln.strip().startswith('file_template')
    )
    template = line.split('=', 1)[1].strip()
    assert not template.endswith('.py'), (
        f'file_template 不能以 .py 结尾（alembic 会自己加扩展名）：{template!r}'
    )
    assert '%(rev)s' in template, f'file_template 必须包含 %(rev)s：{template!r}'


def test_new_migration_script_warns_when_autogenerate_found_nothing() -> None:
    """An empty migration is the failure mode this tree exists to remove.

    autogenerate happily writes a file with empty upgrade()/downgrade() when it
    sees no difference; that file is indistinguishable from a real one in a
    diff review, so the script has to say so.
    """
    src = _code_only(NEW_MIGRATION_SH.read_text(encoding='utf-8'))
    assert "grep -q 'op\\.'" in src, (
        '脚本应该检查生成的迁移里有没有 op. 调用，并对空迁移发出警告'
    )
