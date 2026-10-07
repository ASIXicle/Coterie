#!/usr/bin/env python3
"""Tests for venv-autopatch.py's decisions: which release it picks, what it refuses, how it reads a
config and how it notices hand changes. No network, no root: PyPI data is built here.

    python3 hookandshield/test_venv_autopatch.py
"""
import datetime as dt
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("va", HERE / "venv-autopatch.py")
va = importlib.util.module_from_spec(spec)
spec.loader.exec_module(va)

NOW = dt.datetime(2026, 10, 11, 9, 0, tzinfo=dt.timezone.utc)
PY = f"cp{sys.version_info.major}{sys.version_info.minor}"


def wheel(name, ver, days_old, tag="py3-none-any", yanked=False, requires_python=None):
    up = (NOW - dt.timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return {"packagetype": "bdist_wheel", "filename": f"{name}-{ver}-{tag}.whl",
            "digests": {"sha256": f"{name}{ver}".ljust(64, "0")[:64]}, "upload_time_iso_8601": up,
            "yanked": yanked, "requires_python": requires_python}


def sdist(name, ver, days_old):
    w = wheel(name, ver, days_old)
    return dict(w, packagetype="sdist", filename=f"{name}-{ver}.tar.gz")


def vuln(i, *fixes):
    return {"id": i, "fix_versions": list(fixes)}


class Choose(unittest.TestCase):
    def rel(self, **vers):
        return {v.replace("_", "."): files for v, files in vers.items()}

    def test_oldest_qualifying_release_by_default_newest_on_request(self):
        r = {"2.6.3": [wheel("u", "2.6.3", 300)], "2.6.9": [wheel("u", "2.6.9", 200)],
             "2.7.0": [wheel("u", "2.7.0", 150)], "2.8.0": [wheel("u", "2.8.0", 26)],
             "2.9.0": [wheel("u", "2.9.0", 2)], "3.0.0": [wheel("u", "3.0.0", 40)]}
        ver, wheels = va.choose("2.6.3", [vuln("A", "2.7.0")], r, NOW, 7, {})
        self.assertEqual(ver, "2.7.0")  # 2.6.9 doesn't fix it; the smallest change that does
        self.assertEqual(wheels[0]["filename"], "u-2.7.0-py3-none-any.whl")
        ver, _ = va.choose("2.6.3", [vuln("A", "2.7.0")], r, NOW, 7, {}, prefer="newest")
        self.assertEqual(ver, "2.8.0")  # 2.9.0 is inside the cool-down; 3.0.0 is a new major

    def test_floor_is_the_highest_fix_any_advisory_needs(self):
        r = {"1.0": [wheel("p", "1.0", 99)], "1.2": [wheel("p", "1.2", 50)], "1.5": [wheel("p", "1.5", 30)]}
        ver, _ = va.choose("1.0", [vuln("A", "1.2"), vuln("B", "1.5")], r, NOW, 7, {})
        self.assertEqual(ver, "1.5")

    def test_fix_only_in_a_new_major_is_reported(self):
        r = {"4.57.6": [wheel("t", "4.57.6", 99)], "5.0.0": [wheel("t", "5.0.0", 60)]}
        ver, why = va.choose("4.57.6", [vuln("A", "5.0.0")], r, NOW, 7, {})
        self.assertIsNone(ver)
        self.assertIn("new major", why)

    def test_calendar_versioned_projects_may_cross_a_major(self):
        r = {"25.1.1": [wheel("pip", "25.1.1", 400)], "26.2.1": [wheel("pip", "26.2.1", 60)]}
        self.assertIsNone(va.choose("25.1.1", [vuln("A", "26.0")], r, NOW, 7, {})[0])
        self.assertEqual(va.choose("25.1.1", [vuln("A", "26.0")], r, NOW, 7, {}, calver=True)[0], "26.2.1")
        r = {"2025.8.3": [wheel("certifi", "2025.8.3", 400)], "2026.2.25": [wheel("certifi", "2026.2.25", 200)]}
        self.assertEqual(va.choose("2025.8.3", [vuln("A", "2026.1.4")], r, NOW, 7, {})[0], "2026.2.25")

    def test_an_advisory_without_a_fix_blocks_the_package(self):
        r = {"1.5.9": [wheel("c", "1.5.9", 99)], "1.6.0": [wheel("c", "1.6.0", 30)]}
        ver, why = va.choose("1.5.9", [vuln("A", "1.6.0"), vuln("B")], r, NOW, 7, {})
        self.assertIsNone(ver)
        self.assertIn("no fix released yet", why)

    def test_cool_down(self):
        r = {"1.1.2": [wheel("m", "1.1.2", 99)], "1.2.3": [wheel("m", "1.2.3", 5)]}
        ver, why = va.choose("1.1.2", [vuln("A", "1.2.1")], r, NOW, 7, {})
        self.assertIsNone(ver)
        self.assertIn("days old", why)
        ver, _ = va.choose("1.1.2", [vuln("A", "1.2.1")], r, NOW, 3, {})
        self.assertEqual(ver, "1.2.3")

    def test_a_version_that_failed_here_is_skipped(self):
        r = {"3.11": [wheel("i", "3.11", 99)], "3.19": [wheel("i", "3.19", 20)], "3.20": [wheel("i", "3.20", 17)]}
        ver, _ = va.choose("3.11", [vuln("A", "3.15")], r, NOW, 7, {"3.19": "failed its rehearsal twice"})
        self.assertEqual(ver, "3.20")

    def test_prereleases_yanked_and_sdist_only_are_skipped(self):
        r = {"6.16.2": [wheel("p", "6.16.2", 99)], "6.17.0": [sdist("p", "6.17.0", 40)],
             "6.18.0": [wheel("p", "6.18.0", 30, yanked=True)], "6.19.0rc1": [wheel("p", "6.19.0rc1", 20)]}
        ver, why = va.choose("6.16.2", [vuln("A", "6.17.0")], r, NOW, 7, {})
        self.assertIsNone(ver)
        r["6.18.1"] = [wheel("p", "6.18.1", 25)]
        ver, _ = va.choose("6.16.2", [vuln("A", "6.17.0")], r, NOW, 7, {})
        self.assertEqual(ver, "6.18.1")

    def test_wheel_tags_and_requires_python(self):
        r = {"1.0": [wheel("x", "1.0", 99)],
             "1.1": [wheel("x", "1.1", 30, tag="cp27-cp27mu-manylinux1_x86_64")],
             "1.2": [wheel("x", "1.2", 30, requires_python=">=4.0")]}
        ver, why = va.choose("1.0", [vuln("A", "1.1")], r, NOW, 7, {})
        self.assertIsNone(ver)
        self.assertIn("no wheel", why)


class WheelsAndRequirements(unittest.TestCase):
    def test_all_compatible_wheels_are_offered_to_pip(self):
        files = [wheel("m", "1.2.3", 30, tag=f"{PY}-{PY}-manylinux_2_17_x86_64.manylinux2014_x86_64"),
                 wheel("m", "1.2.3", 30, tag="py3-none-any"), sdist("m", "1.2.3", 30)]
        ws = va.compatible_wheels(files)
        names = [w["filename"] for w in ws]
        self.assertIn("m-1.2.3-py3-none-any.whl", names)
        self.assertNotIn("m-1.2.3.tar.gz", names)
        pick = {"name": "m", "to": "1.2.3", "from": "1.1.2", "wheels": ws, "old_wheels": ws[:1]}
        line = va.req_lines([pick], "to")
        self.assertTrue(line.startswith("m==1.2.3 "))
        self.assertEqual(line.count("--hash=sha256:"), len(ws))


class Ledger(unittest.TestCase):
    def test_first_run_sees_no_drift(self):
        self.assertEqual(va.drift({"freeze": None}, {"a": {"version": "1"}}), {})

    def test_hand_changes_are_noticed(self):
        led = {"freeze": {"a": "1.0", "b": "2.0"}}
        live = {"a": {"version": "1.0"}, "b": {"version": "2.1"}, "c": {"version": "0.1"}}
        self.assertEqual(va.drift(led, live), {"b": "changed 2.0 -> 2.1", "c": "added at 0.1"})


class Config(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        try:
            return va.load_config(f.name)
        finally:
            os.unlink(f.name)

    BASE = 'pip_audit = "/x/pip-audit"\naudit_user = "nobody"\n'

    def test_defaults(self):
        c = self.load(self.BASE + '[[venv]]\nname = "m"\npath = "/opt/m/venv"\nowner = "root"\n'
                      '[venv.holds]\nSome_Pkg = "why"\n')
        v = c["venv"][0]
        self.assertEqual((c["cooldown_days"], v["mode"], v["test_user"], v["umask"]), (7, "auto", "root", "022"))
        self.assertEqual(v["holds"], {"some-pkg": "why"})

    def test_refusals(self):
        # Each case names real users, so it fails for its own reason (asserted by message).
        for bad, why in [('[[venv]]\nname = "m"\npath = "/p"\n', "needs 'owner'"),
                         ('[[venv]]\nname = "m"\npath = "/p"\nowner = "nobody"\nmode = "yolo"\n', "mode must be"),
                         ('[[venv]]\nname = "m"\npath = "/p"\nowner = "nobody"\ntypo_key = 1\n', "unknown keys"),
                         ('[[venv]]\nname = "m"\npath = "/p"\nowner = "nobody"\n[[venv]]\nname = "m"\npath = "/q"\n'
                          'owner = "nobody"\n', "repeated"),
                         ('state_dir = "/var/lib"\n', "dedicated absolute directory")]:
            with self.assertRaises(SystemExit, msg=bad) as cm:
                self.load(self.BASE + bad)
            self.assertIn(why, str(cm.exception))
        with self.assertRaises(SystemExit):
            self.load('audit_user = "nobody"\n')
        for bad in ['pip_audit = "/x"\naudit_user = "root"\n',                                  # root audit
                    self.BASE + '[[venv]]\nname = "m"\npath = "/p"\nowner = "no_such_user_xyz"\n',
                    self.BASE + 'prefer = "random"\n',
                    self.BASE + '[report]\nmailbox = "news"\n',                                  # incomplete
                    self.BASE + '[report]\nmaildir_root = "/r"\nmaildir_user = "root"\nmaildir_py = "/m"\nmailbox = "n"\n',
                    self.BASE + '[report]\nmaildir_root = "/r"\nmaildir_user = "nobody"\nmaildir_py = "/m"\nmailbox = "n"\ntypo = 1\n']:
            with self.assertRaises(SystemExit, msg=bad):
                self.load(bad)


class ImportCheck(unittest.TestCase):
    def test_third_party_imports_only(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "localmod.py").write_text("raise SystemExit('a local module must not be imported')\n")
            Path(d, "s.py").write_text("import os, json\nimport localmod\nfrom packaging.version import Version\n"
                                       "from . import nothing\n")
            p = subprocess.run([sys.executable, "-c", va.IMPORT_CHECK, str(Path(d, "s.py"))],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("packaging", p.stdout)
            # A module no distribution provides is the script's own: named, not imported.
            Path(d, "t.py").write_text("import surely_not_installed_xyz\n")
            p = subprocess.run([sys.executable, "-c", va.IMPORT_CHECK, str(Path(d, "t.py"))],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("not checked: surely_not_installed_xyz", p.stdout)
            # An installed distribution whose module no longer imports fails the check.
            site = Path(d, "site")
            (site / "brokenpkg_xyz-1.0.dist-info").mkdir(parents=True)
            (site / "brokenpkg_xyz-1.0.dist-info" / "METADATA").write_text("Name: brokenpkg-xyz\nVersion: 1.0\n")
            (site / "brokenpkg_xyz-1.0.dist-info" / "top_level.txt").write_text("brokenpkg_xyz\n")
            (site / "brokenpkg_xyz.py").write_text("raise ImportError('broken by an upgrade')\n")
            Path(d, "u.py").write_text("import brokenpkg_xyz\n")
            p = subprocess.run([sys.executable, "-c", va.IMPORT_CHECK, str(Path(d, "u.py"))],
                               capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(site)))
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("broken by an upgrade", p.stderr)


class Refusal(unittest.TestCase):
    """Never a venv interpreter as root, never a venv another uid can write."""
    def venv(self, d, owner=None):
        me = os.environ.get("USER") or __import__("pwd").getpwuid(os.getuid()).pw_name
        root = Path(d, "parent", "v")
        sp = Path(va.site_packages(root))
        sp.mkdir(parents=True)
        (root / "bin").mkdir()
        for p in (Path(d, "parent"), root, root / "bin", root / "lib", sp.parent, sp):
            p.chmod(0o755)
        return {"path": str(root), "owner": owner or me, "test_user": owner or me}

    def test_clean_venv_may_run(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(va.refusal(self.venv(d)), "")

    def test_root_owner_or_tester_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            v = self.venv(d)
            self.assertIn("owner is root", va.refusal(dict(v, owner="root")))
            self.assertIn("test_user is root", va.refusal(dict(v, test_user="root")))

    def test_world_or_shared_group_write_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            v = self.venv(d)
            Path(va.site_packages(v["path"])).chmod(0o777)
            self.assertIn("everyone", va.refusal(v))
            Path(va.site_packages(v["path"])).chmod(0o755)
            Path(v["path"]).parent.chmod(0o757)
            self.assertIn("everyone", va.refusal(v))
            Path(v["path"]).parent.chmod(0o755)
            import grp, pwd
            me = pwd.getpwuid(os.getuid()).pw_name
            shared = next((g for g in map(grp.getgrgid, os.getgroups()) if set(g.gr_mem) - {me, "root"}), None)
            if shared is None:
                self.skipTest("no group with other members to test a shared group write")
            os.chown(Path(v["path"], "bin"), -1, shared.gr_gid)
            Path(v["path"], "bin").chmod(0o775)
            self.assertIn(f"(group {shared.gr_name})", va.refusal(v))


    def test_the_whole_tree_and_every_ancestor_are_checked(self):
        with tempfile.TemporaryDirectory() as d:
            v = self.venv(d)
            root = Path(v["path"])
            (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
            (root / "pyvenv.cfg").chmod(0o644)
            mod = Path(va.site_packages(root), "pkg", "deep.py")
            mod.parent.mkdir()
            mod.parent.chmod(0o755)
            mod.write_text("x = 1\n")
            mod.chmod(0o644)
            self.assertEqual(va.refusal(v), "")
            for target in (root / "lib", root / "pyvenv.cfg", mod):
                old = target.stat().st_mode & 0o777
                target.chmod(old | 0o002)
                self.assertIn(str(target), va.refusal(v), target)
                target.chmod(old)
            Path(d).chmod(0o777)                 # an ancestor: world-writable, not sticky
            self.assertIn(f"{d} is writable by everyone", va.refusal(v))
            Path(d).chmod(0o700)
            self.assertEqual(va.refusal(v), "")

    def test_what_the_venv_points_at_is_checked_too(self):
        with tempfile.TemporaryDirectory() as d:
            v = self.venv(d)
            root = Path(v["path"])
            (root / "bin" / "python3").symlink_to(sys.executable)            # a root-owned interpreter
            (root / "pyvenv.cfg").write_text(f"home = {os.path.dirname(os.path.realpath(sys.executable))}\n")
            (root / "pyvenv.cfg").chmod(0o644)
            self.assertEqual(va.refusal(v), "")
            evil = Path(d, "userbin")                                         # an interpreter a user can write
            evil.mkdir()
            (evil / "python3").write_text("#!/bin/sh\n")
            evil.chmod(0o777)
            (root / "bin" / "python3").unlink()
            (root / "bin" / "python3").symlink_to(evil / "python3")
            self.assertIn("outside the venv", va.refusal(v))
            (root / "bin" / "python3").unlink()
            (root / "bin" / "python3").symlink_to(sys.executable)
            (root / "pyvenv.cfg").write_text(f"home = {evil}\n")
            self.assertIn("outside the venv", va.refusal(v))

    def test_an_acl_is_refused(self):
        import shutil as sh
        if not sh.which("setfacl"):
            self.skipTest("setfacl is not installed")
        with tempfile.TemporaryDirectory() as d:
            v = self.venv(d)
            sp = va.site_packages(v["path"])
            p = subprocess.run(["setfacl", "-m", "u:nobody:rwx", sp], capture_output=True, text=True)
            if p.returncode != 0:
                self.skipTest(f"no ACL support here: {p.stderr.strip()}")
            self.assertIn("has an ACL", va.refusal(v))


class Snippets(unittest.TestCase):
    def test_inspect_reads_dist_info_without_running_the_venv(self):
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d)
            (sp / "plain_pkg-1.2.dist-info").mkdir()
            (sp / "plain_pkg-1.2.dist-info" / "METADATA").write_text("Name: plain-pkg\nVersion: 1.2\n")
            (sp / "edit_pkg-0.1.dist-info").mkdir()
            (sp / "edit_pkg-0.1.dist-info" / "METADATA").write_text("Name: edit-pkg\nVersion: 0.1\n")
            (sp / "edit_pkg-0.1.dist-info" / "direct_url.json").write_text('{"url": "file:///src", "dir_info": {"editable": true}}')
            (sp / "evil.pth").write_text("import os; os.system('touch " + str(sp / "PWNED") + "')\n")
            p = subprocess.run([sys.executable, "-I", "-S", "-c", va.INSPECT, str(sp)], capture_output=True, text=True)
            got = {x["name"]: x for x in __import__("json").loads(p.stdout)}
            self.assertEqual(got["plain-pkg"]["version"], "1.2")
            self.assertIsNone(got["plain-pkg"]["direct_url"])
            self.assertEqual(got["edit-pkg"]["direct_url"]["url"], "file:///src")
            self.assertFalse((sp / "PWNED").exists(), "a .pth in the venv ran")

    def test_inspect_venv_end_to_end(self):
        import pwd
        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as d:
            sp = Path(va.site_packages(d))
            sp.mkdir(parents=True)
            for name, ver in (("torch", "2.11.0+cpu"), ("idna", "3.11")):
                (sp / f"{name}-{ver}.dist-info").mkdir()
                (sp / f"{name}-{ver}.dist-info" / "METADATA").write_text(f"Name: {name}\nVersion: {ver}\n")
            got = va.inspect_venv({"audit_user": me}, d)
            self.assertTrue(got["idna"]["from_pypi"])
            self.assertFalse(got["torch"]["from_pypi"])
            self.assertEqual(got["torch"]["public"], "2.11.0")
            self.assertIn("local build", got["torch"]["why"])

    def test_report_lands_in_the_mailbox_without_a_server(self):
        maildir_py = next(HERE.parent.glob("*/maildir.py"), None)   # the memory server's, beside this tree
        if maildir_py is None:
            self.skipTest("maildir.py not beside this checkout")
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, "-I", "-S", "-c", va.DELIVER, str(maildir_py), d, "news", "subj"],
                               input="the body", capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            new = list(Path(d, "news", "inbox", "new").iterdir())
            self.assertEqual(len(new), 1)
            text = new[0].read_text()
            self.assertIn('"from": "venv-autopatch"', text)
            self.assertIn('"subject": "subj"', text)
            self.assertTrue(text.rstrip().endswith("the body"))


class InUse(unittest.TestCase):
    def test_relative_path_and_activated_venv_are_seen(self):
        with tempfile.TemporaryDirectory() as d:
            fv = Path(d, "fv")
            (fv / "bin").mkdir(parents=True)
            (fv / "bin" / "python3").symlink_to(sys.executable)
            root = os.path.realpath(fv) + "/"
            sleep = ["-c", "import time; time.sleep(30)"]
            procs = {
                "relative path": subprocess.Popen(["fv/bin/python3", *sleep], cwd=d),
                "activated": subprocess.Popen([sys.executable, *sleep], env=dict(os.environ, VIRTUAL_ENV=str(fv))),
                "unrelated": subprocess.Popen([sys.executable, *sleep], env={k: v for k, v in os.environ.items()
                                                                            if k != "VIRTUAL_ENV"}),
            }
            try:
                import time
                time.sleep(0.5)
                seen = {k: va.uses_venv(str(p.pid), root) for k, p in procs.items()}
            finally:
                for p in procs.values():
                    p.kill()
                    p.wait()
            self.assertEqual(seen, {"relative path": True, "activated": True, "unrelated": False})


class AuditCache(unittest.TestCase):
    """pip-audit's default cache is under HOME; a service user's HOME may be /nonexistent."""

    def test_audit_gives_pip_audit_its_own_cache_and_removes_it(self):
        me = va.pwd.getpwuid(os.geteuid()).pw_name
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "pip-audit"
            seen = Path(d) / "seen.txt"
            # Like pip-audit: without a usable --cache-dir it would make one under HOME and fail.
            fake.write_text(f"""#!/bin/sh
c=""; while [ $# -gt 0 ]; do [ "$1" = --cache-dir ] && c=$2; shift; done
[ -n "$c" ] && [ -d "$c" ] && [ -w "$c" ] || {{ echo "PermissionError: $HOME" >&2; exit 2; }}
echo "$c" > {seen}
echo '{{"dependencies": [{{"name": "idna", "version": "3.6", "vulns": [{{"id": "X-1", "fix_versions": ["3.7"]}}]}}]}}'
exit 1
""")
            fake.chmod(0o755)
            cfg = {"state_dir": str(Path(d) / "state"), "audit_user": me, "pip_audit": str(fake)}
            found = va.audit(cfg, {"idna": "3.6"})
            self.assertEqual(found, {"idna": [{"id": "X-1", "fix_versions": ["3.7"]}]})
            cache = Path(seen.read_text().strip())
            self.assertTrue(str(cache).startswith(cfg["state_dir"]), cache)
            self.assertFalse(cache.exists(), "the per-run cache is removed")

    def test_the_scanner_passes_a_cache_dir_too(self):
        # beside this file in the source tree; under newstron/ in the release layout
        src = next(p for p in (HERE / "pip-audit-scan.py", HERE.parent / "newstron" / "pip-audit-scan.py")
                   if p.exists()).read_text()
        self.assertIn('"--cache-dir", cache_dir]', src)
        self.assertIn("shutil.rmtree(cache_dir, ignore_errors=True)", src)


class WhatReachesTheOperator(unittest.TestCase):
    """Open advisories reach a person, not just the news box."""

    def test_never_applied_and_audit_only_every_time(self):
        for outcome in ("not applied", "partly patched", "audit only"):
            self.assertTrue(va.needs_attention(outcome, None), outcome)
            self.assertTrue(va.needs_attention(outcome, outcome), outcome)

    def test_deferred_only_twice_running(self):
        self.assertFalse(va.needs_attention("deferred", None))
        self.assertFalse(va.needs_attention("deferred", "patched"))
        self.assertTrue(va.needs_attention("deferred", "deferred"))

    def test_quiet_outcomes_stay_quiet(self):
        for outcome in ("nothing to apply", "patched", "plan", "report only", "failed", "rolled back"):
            self.assertFalse(va.needs_attention(outcome, "deferred"), outcome)  # failures route by `bad`

    def test_routing(self):
        r = {"mailbox": "news", "failures_mailbox": "operator"}
        self.assertEqual(va.report_route(r, 0, []), ("venv-autopatch: weekly report", ["news"]))
        s, b = va.report_route(r, 0, ["cad not applied"])
        self.assertEqual((b, "needs attention" in s and "cad not applied" in s), (["news", "operator"], True))
        s, b = va.report_route(r, 1, ["cad not applied"])
        self.assertEqual((b, "FAILURES" in s), (["news", "operator"], True))
        self.assertEqual(va.report_route({"mailbox": "news"}, 1, []), ("venv-autopatch: FAILURES, see the report", ["news"]))

    def test_the_run_feeds_them(self):
        src = (HERE / "venv-autopatch.py").read_text()
        self.assertIn("if apply and needs_attention(outcome, previous):", src)
        self.assertIn("deliver(cfg, text, bad, attention)", src)
        self.assertIn("subject, boxes = report_route(r, bad, attention)", src)


class StopAndProcessGroups(unittest.TestCase):
    """Ctrl-C and SIGHUP take the rollback path; a killed runuser must not leave pip running."""

    def setUp(self):
        import signal
        self.signal = signal
        self.saved = {s: signal.getsignal(s) for s in va.STOP_SIGNALS}
        self.addCleanup(self.restore)

    def restore(self):
        for s, h in self.saved.items():
            self.signal.signal(s, h)
        va.STOPPING = False

    def test_every_stop_signal_raises_terminated_and_ignores_the_rest(self):
        s = self.signal
        self.assertEqual(set(va.STOP_SIGNALS), {s.SIGTERM, s.SIGINT, s.SIGHUP})
        for sig in va.STOP_SIGNALS:
            self.restore()
            with self.assertRaises(va.Terminated) as cm:
                va.on_sigterm(sig, None)
            self.assertIn(s.Signals(sig).name, str(cm.exception))
            self.assertTrue(va.STOPPING)
            for other in va.STOP_SIGNALS:
                self.assertIs(s.getsignal(other), s.SIG_IGN)

    def test_a_run_registers_all_three(self):
        src = (HERE / "venv-autopatch.py").read_text()
        self.assertIn("for s in STOP_SIGNALS:\n        signal.signal(s, on_sigterm)", src)

    def _grandchild(self, d):
        # sh forks a sleep and waits, like runuser forking pip: the sleep is a grandchild of this side.
        pidfile = Path(d) / "pid"
        argv = ["sh", "-c", f'sleep 60 & echo $! > {pidfile}; wait']
        return pidfile, argv

    def _gone(self, pidfile):
        import time
        pid = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        os.kill(pid, 9)
        return False

    def test_a_timeout_kills_the_whole_group(self):
        me = va.pwd.getpwuid(os.geteuid()).pw_name
        with tempfile.TemporaryDirectory() as d:
            pidfile, argv = self._grandchild(d)
            with self.assertRaises(subprocess.TimeoutExpired):
                va.run_as(me, "022", argv, cwd=d, timeout=1)
            self.assertTrue(self._gone(pidfile), "the grandchild outlived the timeout")

    def test_an_exception_while_waiting_kills_the_whole_group(self):
        s = self.signal
        me = va.pwd.getpwuid(os.geteuid()).pw_name

        def boom(signum, frame):
            raise va.Terminated("test stop")
        old = s.signal(s.SIGALRM, boom)
        self.addCleanup(s.signal, s.SIGALRM, old)
        with tempfile.TemporaryDirectory() as d:
            pidfile, argv = self._grandchild(d)
            s.alarm(1)
            with self.assertRaises(va.Terminated):
                va.run_as(me, "022", argv, cwd=d, timeout=30)
            s.alarm(0)
            self.assertTrue(self._gone(pidfile), "the grandchild outlived the exception")

    def test_the_root_branch_names_runuser_by_absolute_path(self):
        # The first root run died because a bare "runuser" was looked up on the minimal PATH, which has no
        # sbin. This branch can't run without root, so check what it would exec.
        self.assertTrue(os.path.isabs(va.RUNUSER) and os.access(va.RUNUSER, os.X_OK), va.RUNUSER)
        src = (HERE / "venv-autopatch.py").read_text()
        self.assertNotIn('["runuser"', src)
        self.assertIn("wrapped = [RUNUSER,", src)

    def test_output_and_input_still_pass_through(self):
        me = va.pwd.getpwuid(os.geteuid()).pw_name
        p = va.run_as(me, "022", ["sh", "-c", "cat; echo err >&2; exit 3"], input_text="in\n")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (3, "in\n", "err\n"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
