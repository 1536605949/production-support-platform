# -*- coding: utf-8 -*-
"""Guards for the release gate's own wiring.

The gate is the thing that is supposed to catch defects, so a defect *in the gate*
is the most expensive kind here: it either passes while nothing was verified, or
fails for a reason unrelated to the change and gets ignored.

The specific one this file locks down: the UI layer used to be invoked as
``node scripts/ui_smoke_test.mjs`` straight from ``release_gate.py``, which made
its verdict depend on an ambient ``NODE_PATH`` that nothing set or documented.
In a clean shell the step exited 2 and the gate reported

    命令失败（exit 2）: node scripts/ui_smoke_test.mjs https://.../

which reads like a UI regression rather than a missing npm package. Now one
wrapper resolves jsdom and every entry point goes through it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / 'scripts' / 'release_gate.py'
WRAPPER = ROOT / 'scripts' / 'ui_smoke.sh'
MAKEFILE = ROOT / 'Makefile'
CUSTOMER_SMOKE = ROOT / 'scripts' / 'ui_smoke_customer.mjs'


def _code_only(src: str) -> str:
    """Source with comment lines removed.

    A guard that a comment can satisfy guards nothing — see
    ``tests/test_migration_tooling.py``, where the mount assertion stayed green
    after the mount was deleted because the comment explaining it contained the
    same text.

    Both ``#`` and ``//`` are stripped, because this module asserts things about
    ``.mjs`` files as well, and a ``//`` comment is exactly as able to satisfy a
    guard as a ``#`` one. Neither pair is meaningful at the start of a line in
    Python, shell or Make, so stripping both is safe for every caller here.
    """
    return '\n'.join(
        line for line in src.splitlines()
        if not line.lstrip().startswith(('#', '//'))
    )


def _run_wrapper(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('没有 bash，跳过 UI 冒烟包装脚本测试')
    return subprocess.run(
        [bash, WRAPPER.relative_to(ROOT).as_posix(), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )


def test_ui_layer_goes_through_the_jsdom_wrapper() -> None:
    """The gate must not invoke the UI test's node entry point directly.

    Calling node directly is how the step came to depend on ambient NODE_PATH.
    """
    src = _code_only(GATE.read_text(encoding='utf-8'))
    assert 'scripts/ui_smoke.sh' in src, (
        'release_gate.py 的 UI 层应该走 scripts/ui_smoke.sh（它负责定位 jsdom）'
    )
    assert 'ui_smoke_test.mjs' not in src, (
        'release_gate.py 不应该直接调 ui_smoke_test.mjs —— 那样会重新依赖外部 NODE_PATH'
    )


def test_gate_reports_a_missing_bash_instead_of_crashing() -> None:
    """``subprocess.run(['bash', ...])`` with no bash raises FileNotFoundError.

    That would abort the gate with a traceback instead of a named failure, which
    is exactly the "fails for an unrelated reason" pattern.
    """
    src = _code_only(GATE.read_text(encoding='utf-8'))
    assert "shutil.which('bash')" in src, (
        'release_gate.py 应该先检查 bash 是否存在，否则会抛 FileNotFoundError'
    )


def test_gate_passes_the_resolved_bash_path_not_the_bare_name() -> None:
    """A bare ``'bash'`` finds the WSL stub on Windows, not Git Bash.

    ``CreateProcess`` searches System32 *before* PATH, so ``subprocess.run(['bash',
    ...])`` resolves to ``C:\\Windows\\System32\\bash.exe`` even when Git Bash is
    first in PATH and ``shutil.which`` returns it. Observed on this machine: the
    gate failed with exit 1 and "PROGRAM BLOCKED BY SECURITY POLICY ... wsl.exe"
    while the identical command typed by hand worked — a 4-minute gate failing for
    a reason that has nothing to do with the change.
    """
    src = _code_only(GATE.read_text(encoding='utf-8'))
    assert "run([bash, 'scripts/ui_smoke.sh'" in src, (
        '要用 shutil.which 解析出的绝对路径调用 bash；'
        "裸名 'bash' 在 Windows 上会命中 System32 里的 WSL 存根"
    )
    assert "run(['bash'" not in src, (
        "不要用裸名 'bash' 起子进程 —— Windows 的 CreateProcess 会先搜 System32"
    )


def test_makefile_ui_uses_the_wrapper() -> None:
    """``make ui`` is the documented entry point; it needs the same resolution."""
    src = _code_only(MAKEFILE.read_text(encoding='utf-8'))
    target = src.split('\nui:', 1)[1].split('\n\n', 1)[0]
    assert 'scripts/ui_smoke.sh' in target, (
        'make ui 应该调用 scripts/ui_smoke.sh'
    )
    assert 'node scripts/ui_smoke_test.mjs' not in target, (
        'make ui 不应该直接调 node —— 干净 shell 里会因为缺 jsdom 而 exit 2'
    )


def test_ui_smoke_wrapper_requires_a_url() -> None:
    """Deterministic branch, no jsdom needed: a missing URL is a usage error."""
    proc = _run_wrapper()
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert '用法' in proc.stderr, proc.stderr


def test_ui_smoke_wrapper_names_the_fixes_for_a_missing_jsdom() -> None:
    """The point of the wrapper is the message, so assert the message.

    ``exit 2`` on its own was the original problem: it named nothing.
    """
    src = _code_only(WRAPPER.read_text(encoding='utf-8'))
    for hint in ('npm i jsdom', 'JSDOM_HOME', 'NODE_PATH'):
        assert hint in src, f'找不到 jsdom 时的提示里应该给出 {hint!r} 这个解法'


def test_ui_smoke_wrapper_forces_the_trailing_slash() -> None:
    """The page lives under a path prefix; a bare host makes relative fetches 404."""
    src = _code_only(WRAPPER.read_text(encoding='utf-8'))
    assert 'URL="${URL%/}/"' in src, (
        'wrapper 应该把 URL 规范化成以 / 结尾，否则页面里的相对请求会解析到上一层'
    )


def test_the_c_end_page_has_its_own_dom_layer() -> None:
    """Two frontends with *opposite* contracts on the same data.

    The operator console is required to show intent, confidence, sources and the
    trace id. The C-end page is required to show none of them. One UI layer
    cannot assert both, so there are two — and dropping the second would be
    invisible: the gate stays green while nothing at all checks the page a
    shopper actually uses.
    """
    src = _code_only(GATE.read_text(encoding='utf-8'))
    assert 'scripts/ui_smoke_customer.mjs' in src, (
        'release_gate.py 应该把 C 端页面冒烟也跑上 —— 它断言的是「不渲染内部字段」'
    )


def test_customer_smoke_reads_rendered_text_not_the_inline_script() -> None:
    """``document.body.textContent`` includes the page's own ``<script>``.

    The page is a single file, so its script — comments included — sits inside
    ``<body>``. The first version of the C-end smoke reported four phantom leaks
    (``intent``, ``confidence``, ``get_sku``, ``get_order``) for exactly this
    reason: the page's explanatory comment names all four. A negative assertion
    that reads source is worse than none, because it gets "fixed" by deleting
    the comment.

    These two literals are the code that fixes it. They are asserted as code
    lines rather than via a "does the file mention textContent" check, since the
    comment above this test would satisfy that.
    """
    src = CUSTOMER_SMOKE.read_text(encoding='utf-8')
    assert 'const text = () => visibleText(doc.body);' in src, (
        'C 端冒烟必须用 visibleText() 取「真正渲染出来的文本」，'
        '不能直接用 document.body.textContent（那会把内联脚本源码算进去）'
    )
    assert "tag === 'SCRIPT'" in src, (
        'visibleText() 必须跳过 SCRIPT/STYLE，否则负向断言全是空的'
    )


def test_smoke_scripts_do_not_label_code_units_as_bytes() -> None:
    """``html.length`` is UTF-16 code units, not bytes.

    Both pages are mostly Chinese, so the two figures differ by roughly two per
    CJK character: the C-end page is 15247 bytes but 14829 characters. A log
    line that prints the character count next to the word "字节" therefore reads
    as "the deployed page is smaller than the file on disk" — which looks like
    staleness, and is not. The byte figure is the one ``docs/PRODUCT.md`` and
    ``scripts/verify_product_doc.py`` assert, so it has to be printed too.

    Written as "every line that says 字节 must also compute byteLength" rather
    than the cheaper "the file mentions byteLength": the cheap version is
    satisfied by a comment, and both files' comments now mention it.
    """
    for path in (ROOT / 'scripts' / 'ui_smoke_test.mjs', CUSTOMER_SMOKE):
        src = _code_only(path.read_text(encoding='utf-8'))
        for line in src.splitlines():
            if '字节' in line:
                assert 'byteLength' in line, (
                    f'{path.name} 把字符数标成了「字节」: {line.strip()!r} —— '
                    'html.length 是 UTF-16 码元数，不是字节数'
                )
