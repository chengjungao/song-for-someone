# -*- coding: utf-8 -*-
"""环境自检里「找便携包」那一层的测试。

覆盖 doctor 的探测能力：候选目录的优先级、哪套 Python 算可用、site-packages
怎么推。全程不碰真实显卡，也不依赖本机装没装便携包 —— 都用临时目录造假结构。
碰到会扫到各处候选的用例，一律把候选目录限死，免得测试变成在断言开发机的环境。
"""

from __future__ import annotations

import platform
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from song_for_someone import doctor


def make_portable(
    root,
    dirname="python_embeded",
    with_torch=True,
    with_acestep=True,
    layout="windows",
):
    """在 ``root`` 下造一个假的便携包。

    :param layout: ``windows`` 走 ``python_embeded/python.exe`` 配
        ``Lib/site-packages``；``posix`` 走 ``python_embeded/bin/python3`` 配
        上一层的 ``lib/site-packages``。
    """
    root = Path(root)
    python_dir = root / dirname
    python_dir.mkdir(parents=True, exist_ok=True)

    if layout == "posix":
        exe = python_dir / "bin" / "python3"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"fake")
        site = python_dir / "lib" / "site-packages"
    else:
        exe = python_dir / "python.exe"
        exe.write_bytes(b"fake")
        site = python_dir / "Lib" / "site-packages"

    if with_torch:
        site.mkdir(parents=True, exist_ok=True)
        (site / "torch").mkdir(exist_ok=True)
    if with_acestep:
        (root / "acestep").mkdir(parents=True, exist_ok=True)
    return root


def run_check(fn, *args):
    """跑一项检查，返回那一条结果。"""
    report = doctor.DoctorReport()
    fn(report, *args)
    return report.checks[0]


def only_candidates(*paths):
    """把候选目录限死成给定的这几个。

    不这么做的话，``find_package_root`` 会继续翻各盘根目录，扫到本机真实存在
    的便携包，测试就变成在断言开发机的环境了。
    """
    return mock.patch.object(doctor, "candidate_roots", return_value=list(paths))


# ---------------------------------------------------------------- 候选目录


class TestCandidateRoots(unittest.TestCase):
    """候选目录的枚举与优先级。"""

    def test_hint_comes_first(self):
        roots = doctor.candidate_roots("/somewhere/ACE")
        self.assertEqual(roots[0], Path("/somewhere/ACE"))

    def test_covers_common_landings(self):
        """``Works/ACE-Step-1.5-portable`` 这类落点必须在候选里。

        自检以前只查当前目录和环境变量，于是「引擎自己找得到、自检说没有」。
        """
        blob = [str(p).lower().replace("\\", "/") for p in doctor.candidate_roots()]
        self.assertTrue(
            any("works/ace-step" in s for s in blob),
            f"候选里没有 Works/ACE-Step-1.5-portable：{blob[:8]}",
        )

    def test_includes_cwd(self):
        roots = [p.resolve() for p in doctor.candidate_roots()]
        self.assertIn(Path.cwd().resolve(), roots)

    def test_deduplicated(self):
        roots = doctor.candidate_roots()
        keys = [str(p).lower() for p in roots]
        self.assertEqual(len(keys), len(set(keys)), "候选目录里有重复")

    def test_hint_duplicate_folded_into_one(self):
        roots = doctor.candidate_roots(str(Path.cwd()))
        cwd = Path.cwd().resolve()
        self.assertEqual(
            len([p for p in roots if p.resolve() == cwd]), 1,
            "指定了当前目录时，它应该只出现一次",
        )


# ---------------------------------------------------------------- Python 定位


class TestSitePackagesOf(unittest.TestCase):
    """由 Python 可执行文件推 site-packages。"""

    def test_windows_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            exe = root / "python_embeded" / "python.exe"
            self.assertIn(
                root / "python_embeded" / "Lib" / "site-packages",
                doctor.site_packages_of(exe),
            )

    def test_posix_layout_looks_one_level_up(self):
        """``bin/python3`` 的 site-packages 在上一层的 lib 里。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, layout="posix")
            exe = root / "python_embeded" / "bin" / "python3"
            self.assertIn(
                root / "python_embeded" / "lib" / "site-packages",
                doctor.site_packages_of(exe),
            )


class TestHasTorch(unittest.TestCase):
    def test_true_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            self.assertTrue(doctor.has_torch(root / "python_embeded" / "python.exe"))

    def test_false_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            self.assertFalse(doctor.has_torch(root / "python_embeded" / "python.exe"))

    def test_does_not_import_torch(self):
        """只看目录，不起子进程 —— 自检不该为了这个多花几秒。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            with mock.patch.object(doctor.subprocess, "run") as run:
                doctor.has_torch(root / "python_embeded" / "python.exe")
            run.assert_not_called()


class TestFindEmbeddedPython(unittest.TestCase):
    """找那套真能干活的 Python。"""

    def test_finds_windows_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            found = doctor.find_embedded_python(root)
            self.assertEqual(found, root / "python_embeded" / "python.exe")

    def test_finds_posix_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, layout="posix")
            found = doctor.find_embedded_python(root)
            self.assertEqual(found, root / "python_embeded" / "bin" / "python3")

    def test_both_spellings_accepted(self):
        """上游脚本写 python_embedded，实际目录是 python_embeded，两个都得认。"""
        for dirname in ("python_embeded", "python_embedded"):
            with tempfile.TemporaryDirectory() as tmp:
                root = make_portable(tmp, dirname=dirname)
                self.assertIsNotNone(
                    doctor.find_embedded_python(root), f"没认出来：{dirname}"
                )

    def test_lenient_accepts_python_without_torch(self):
        """宽松档：找到就行，「没 torch」交给上层当成一个具体问题报出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            self.assertIsNotNone(doctor.find_embedded_python(root))

    def test_strict_rejects_python_without_torch(self):
        """严格档：起服务时找到一套没 torch 的 Python 毫无意义。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            self.assertIsNone(
                doctor.find_embedded_python(root, require_torch=True)
            )

    def test_ignores_uv_venv_shell(self):
        """uv 建的那个 .venv 不在认的目录名里，永远不会被选中。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "portable"
            (root / ".venv" / "Scripts").mkdir(parents=True)
            (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"fake")
            (root / "acestep").mkdir(parents=True)
            self.assertIsNone(doctor.find_embedded_python(root, require_torch=True))

    def test_none_for_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(doctor.find_embedded_python(Path(tmp)))


class TestFindPackageRoot(unittest.TestCase):
    """定位便携包根目录。"""

    def test_hint_points_at_portable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            with only_candidates(root):
                self.assertEqual(doctor.find_package_root(str(root)), root.resolve())

    def test_first_usable_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_portable(Path(tmp) / "first")
            second = make_portable(Path(tmp) / "second")
            with only_candidates(first, second):
                self.assertEqual(doctor.find_package_root(), first.resolve())

    def test_requires_acestep_marker(self):
        """光有 Python 不算便携包，还得有 acestep 包。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_acestep=False)
            with only_candidates(root):
                self.assertIsNone(doctor.find_package_root())

    def test_lenient_finds_shell_strict_rejects_it(self):
        """同一份「有 Python 但没 torch」的目录，两档判据结果不同。

        自检要宽松，才能报出「找到了哪套、缺什么」；起服务要严格。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            with only_candidates(root):
                self.assertIsNotNone(doctor.find_package_root())
                self.assertIsNone(
                    doctor.find_package_root(require_torch=True)
                )

    def test_none_when_nothing_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            with only_candidates(Path(tmp) / "nope"):
                self.assertIsNone(doctor.find_package_root())


class TestFindSitePackages(unittest.TestCase):
    def test_returns_site_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            self.assertEqual(
                doctor.find_site_packages(root),
                root / "python_embeded" / "Lib" / "site-packages",
            )

    def test_works_with_long_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, dirname="python_embedded")
            self.assertEqual(
                doctor.find_site_packages(root),
                root / "python_embedded" / "Lib" / "site-packages",
            )

    def test_none_without_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            self.assertIsNone(doctor.find_site_packages(root))


# ---------------------------------------------------------------- 检查项


class TestCheckEmbeddedPython(unittest.TestCase):
    """这一项专门报「用的是哪套 Python」，重点是那套空壳要说清楚。"""

    def test_ok_when_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            check = run_check(doctor.check_embedded_python, root)
            self.assertEqual(check.level, doctor.LEVEL_OK)
            self.assertIn("python_embeded", check.detail)
            self.assertIn("torch", check.detail)

    def test_warn_when_python_lacks_torch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            check = run_check(doctor.check_embedded_python, root)
            self.assertEqual(check.level, doctor.LEVEL_WARN)
            self.assertIn("torch", check.detail)
            self.assertTrue(check.fix)

    def test_venv_shell_is_named_and_explained(self):
        """只剩 .venv 空壳时，要指名道姓，还要说清上游那个拼写坑。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "portable"
            (root / ".venv" / "Scripts").mkdir(parents=True)
            (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"fake")
            (root / "acestep").mkdir(parents=True)

            check = run_check(doctor.check_embedded_python, root)
            self.assertEqual(check.level, doctor.LEVEL_WARN)
            self.assertIn(".venv", check.detail)
            self.assertIn("python_embedded", check.fix, "没把上游的拼写坑写出来")

    def test_warn_when_no_python_dir_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "portable"
            (root / "acestep").mkdir(parents=True)
            check = run_check(doctor.check_embedded_python, root)
            self.assertEqual(check.level, doctor.LEVEL_WARN)
            self.assertIn("python_embeded", check.detail)

    def test_skip_without_root(self):
        check = run_check(doctor.check_embedded_python, None)
        self.assertEqual(check.level, doctor.LEVEL_SKIP)


class TestPackageRootCheck(unittest.TestCase):
    def test_ok_reports_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            check = run_check(doctor.check_package_root, root)
            self.assertEqual(check.level, doctor.LEVEL_OK)
            self.assertEqual(check.detail, str(root))

    def test_missing_root_lists_searched_places(self):
        """找不到时要说清「已经找过哪些地方」，不然用户不知道往哪放。"""
        check = run_check(doctor.check_package_root, None)
        self.assertEqual(check.level, doctor.LEVEL_SKIP)
        self.assertIn("ACE-Step", check.fix)


@unittest.skipUnless(platform.system() == "Windows", "triton 补丁只对 Windows 有意义")
class TestTritonPatchUsesSharedLocator(unittest.TestCase):
    """补丁文件要从「和 Python 同级」的 site-packages 里找，不能写死路径。"""

    def _write_patch(self, site):
        site.mkdir(parents=True, exist_ok=True)
        (site / "sitecustomize.py").write_text(
            f"# {doctor.PATCH_MARKER}\n", encoding="utf-8"
        )

    def test_reads_patch_under_short_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            self._write_patch(root / "python_embeded" / "Lib" / "site-packages")
            check = run_check(doctor.check_triton_patch, root)
            self.assertEqual(check.level, doctor.LEVEL_OK)

    def test_reads_patch_under_long_spelling(self):
        """写死 python_embeded 的老代码在这种布局下只会 SKIP。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, dirname="python_embedded")
            self._write_patch(root / "python_embedded" / "Lib" / "site-packages")
            check = run_check(doctor.check_triton_patch, root)
            self.assertEqual(check.level, doctor.LEVEL_OK)


class TestDiffusersImportUsesSharedLocator(unittest.TestCase):
    def test_out_of_torch_python_still_gets_imported(self):
        """找到的 Python 没 torch 时不该 SKIP —— 让真实的导入错误说出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            with mock.patch.object(doctor.subprocess, "run") as run:
                run.return_value = mock.Mock(
                    returncode=1, stdout="", stderr="No module named 'torch'"
                )
                check = run_check(doctor.check_diffusers_import, root)
            self.assertEqual(check.level, doctor.LEVEL_FAIL)
            self.assertIn("torch", check.detail)
            # 用的必须是便携包那套 Python，不是当前进程的
            called = run.call_args[0][0]
            self.assertIn("python_embeded", str(called[0]))

    def test_skip_when_no_usable_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "portable"
            (root / "acestep").mkdir(parents=True)
            check = run_check(doctor.check_diffusers_import, root)
            self.assertEqual(check.level, doctor.LEVEL_SKIP)


# ---------------------------------------------------------------- 串起来


class TestRunDoctorWiring(unittest.TestCase):
    """新项要真的进报告，顺序也要对。"""

    def _run_with_fake_root(self, root):
        """跑一次完整自检，但把几项慢检查替换掉（显卡、导入、落盘）。"""
        with mock.patch.object(doctor, "find_package_root", return_value=root), \
             mock.patch.object(doctor, "check_gpu"), \
             mock.patch.object(doctor, "check_service"), \
             mock.patch.object(doctor, "check_diffusers_import"), \
             mock.patch.object(doctor, "check_output_dir"):
            return doctor.run_doctor()

    def test_embedded_python_check_is_wired_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            report = self._run_with_fake_root(root)

            keys = [c.key for c in report.checks]
            self.assertIn("embedded-python", keys)
            self.assertIn("package", keys)
            self.assertIn("triton-patch", keys)

            self.assertLess(
                keys.index("package"), keys.index("embedded-python"),
                "便携包要先报，才谈得上里面那套 Python",
            )
            self.assertLess(
                keys.index("embedded-python"), keys.index("triton-patch"),
                "得先确认哪套 Python，才谈得上它上面打的补丁",
            )

    def test_reports_ok_for_complete_portable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            report = self._run_with_fake_root(root)
            check = next(c for c in report.checks if c.key == "embedded-python")
            self.assertEqual(check.level, doctor.LEVEL_OK)


if __name__ == "__main__":
    unittest.main()
