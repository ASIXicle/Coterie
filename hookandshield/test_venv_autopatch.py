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


def pkg(name, ver, why=""):
    return {"name": name, "version": ver, "from_pypi": not why, "why": why, "public": None}


# aiohttp 3.14.4's Requires-Dist as PyPI records it; markers and extras included.
AIOHTTP_3_14_4 = [
    'aiohappyeyeballs>=2.5.0', 'aiosignal>=1.4.0', 'async-timeout<6.0,>=4.0; python_version < "3.11"',
    'attrs>=17.3.0', 'frozenlist>=1.1.1', 'multidict<7.0,>=4.5', 'propcache>=0.2.0',
    'typing_extensions>=4.4; python_version < "3.13"', 'yarl<2.0,>=1.25.1',
    'aiodns>=3.3.0; (sys_platform != "android" and sys_platform != "ios") and extra == "speedups"',
    'Brotli>=1.2; (platform_python_implementation == "CPython" and sys_platform != "android" and '
    'sys_platform != "ios") and extra == "speedups"']
YARL_1_25_1 = ['idna>=2.0', 'multidict>=4.0', 'propcache>=0.2.1']


class DependencyUpgrades(unittest.TestCase):
    """aiohttp 3.14.4 needs yarl >= 1.25.1, and the tool installs with --no-deps, so against yarl 1.24.5
    it failed its rehearsal. The upgrade a fix needs now travels with it, under the same rules."""

    def setUp(self):
        self.cfg = {"cooldown_days": 7}
        self.v = {"holds": {}}
        self.led = {"failed": {}, "user_managed": {}}
        # A venv with yarl 1.24.5 (the parts aiohttp needs), plus the two only older Pythons need.
        self.live = {canon: pkg(name, ver) for canon, name, ver in [
            ("aiohttp", "aiohttp", "3.14.3"), ("yarl", "yarl", "1.24.5"), ("multidict", "multidict", "6.9.1"),
            ("propcache", "propcache", "0.5.2"), ("idna", "idna", "3.19"), ("aiosignal", "aiosignal", "1.4.0"),
            ("aiohappyeyeballs", "aiohappyeyeballs", "2.6.1"), ("attrs", "attrs", "25.4.0"),
            ("frozenlist", "frozenlist", "1.8.0"), ("typing-extensions", "typing_extensions", "4.15.0"),
            ("async-timeout", "async-timeout", "5.0.1")]}
        self.requires = {("aiohttp", "3.14.4"): AIOHTTP_3_14_4, ("yarl", "1.25.1"): YARL_1_25_1,
                         ("yarl", "1.25.3"): YARL_1_25_1, ("yarl", "1.26.0"): YARL_1_25_1, ("multidict", "6.10.0"): []}
        self.releases = {"yarl": {"1.24.5": [wheel("yarl", "1.24.5", 99)], "1.25.1": [wheel("yarl", "1.25.1", 36)],
                                  "1.25.3": [wheel("yarl", "1.25.3", 20)],
                                  "1.26.0": [wheel("yarl", "1.26.0", 2)], "2.0.0": [wheel("yarl", "2.0.0", 30)]},
                         "multidict": {"6.9.1": [wheel("multidict", "6.9.1", 99)],
                                       "6.10.0": [wheel("multidict", "6.10.0", 20)]},
                         "idna": {"2.10": [wheel("idna", "2.10", 900)], "3.19": [wheel("idna", "3.19", 99)]}}

    def deps(self, name="aiohttp", ver="3.14.4"):
        return va.dependency_upgrades(self.cfg, self.v, self.led, self.live, name, ver, NOW,
                                      requires_of=lambda n, v: self.requires[(n, v)],
                                      releases_of=lambda n: self.releases[n])

    def test_the_cad_case_brings_the_oldest_yarl_that_satisfies_it(self):
        deps, why = self.deps()
        self.assertEqual(why, "")
        self.assertEqual([(d["name"], d["from"], d["to"], d["for"]) for d in deps],
                         [("yarl", "1.24.5", "1.25.1", "aiohttp 3.14.4")])   # not 1.26.0 (cool-down) or 2.0.0
        self.assertEqual(deps[0]["old_wheels"][0]["filename"], "yarl-1.24.5-py3-none-any.whl")

    def test_a_satisfied_venv_needs_nothing(self):
        self.live["yarl"] = pkg("yarl", "1.25.1")   # a venv that already has it takes aiohttp 3.14.4 as it is
        self.assertEqual(self.deps(), ([], ""))

    def test_markers_and_extras_that_dont_apply_are_left_out(self):
        del self.live["async-timeout"]              # needed below Python 3.11 only
        if sys.version_info >= (3, 13):
            del self.live["typing-extensions"]      # needed below 3.13 only
        deps, why = self.deps()                     # aiodns and Brotli are extras: never installed here
        self.assertEqual((len(deps), why), (1, ""))

    def test_the_upgrades_are_followed_through_their_own_requirements(self):
        self.requires[("yarl", "1.25.1")] = ['idna>=2.0', 'multidict>=6.10', 'propcache>=0.2.1']
        deps, why = self.deps()
        self.assertEqual(why, "")
        self.assertEqual([(d["name"], d["to"], d["for"]) for d in deps],
                         [("yarl", "1.25.1", "aiohttp 3.14.4"), ("multidict", "6.10.0", "yarl 1.25.1")])

    def test_what_is_reported_instead(self):
        cases = [
            (lambda: self.requires.__setitem__(("aiohttp", "3.14.4"), AIOHTTP_3_14_4 + ["newdep>=1"]),
             "isn't installed here"),
            (lambda: self.v["holds"].__setitem__("yarl", "pinned by hand"), "held in the config: pinned by hand"),
            (lambda: self.led["user_managed"].__setitem__("yarl", "changed outside this tool"), "user-managed"),
            (lambda: self.live.__setitem__("yarl", pkg("yarl", "1.24.5", why="installed from file:///src")),
             "yarl is installed from file:///src"),
            # Only 1.25.1 is in the major and it is in the cool-down; 2.0.0 is out of it and satisfies an
            # unbounded requirement, but is never taken.
            (lambda: (self.releases.__setitem__("yarl", {"1.24.5": [wheel("yarl", "1.24.5", 99)],
                                                         "1.25.1": [wheel("yarl", "1.25.1", 3)],
                                                         "2.0.0": [wheel("yarl", "2.0.0", 30)]}),
                      self.requires.__setitem__(("aiohttp", "3.14.4"), ["yarl>=1.25.1"])), "days old"),
            (lambda: self.requires.__setitem__(("aiohttp", "3.14.4"), ["yarl>=2.0"]), "new major"),
            (lambda: self.requires.__setitem__(("aiohttp", "3.14.4"), ["idna<3"]), "downgrade"),
            (lambda: self.releases["yarl"].__setitem__("1.24.5", [sdist("yarl", "1.24.5", 99)]), "roll back"),
            (lambda: self.requires.__setitem__(("aiohttp", "3.14.4"), ["yarl>=1.25.1 ; nonsense ="]),
             "could not read aiohttp 3.14.4's requirements"),
            (lambda: self.requires.__setitem__(("yarl", "1.25.1"), ["aiohttp<3.14.4"]),   # 3.14.3 would do
             "already moves aiohttp to 3.14.4"),
        ]
        for change, why in cases:
            self.setUp()
            change()
            deps, got = self.deps()
            self.assertIsNone(deps, why)
            self.assertIn(why, got)

    def test_a_dependency_that_failed_here_is_skipped(self):
        self.releases["yarl"]["1.25.2"] = [wheel("yarl", "1.25.2", 20)]
        self.requires[("yarl", "1.25.2")] = YARL_1_25_1
        self.led["failed"]["yarl"] = {"1.25.1": "failed its rehearsal twice"}
        deps, _ = self.deps()
        self.assertEqual(deps[0]["to"], "1.25.2")

    def test_the_batch_installs_and_rolls_back_the_upgrade_with_the_fix(self):
        deps, _ = self.deps()
        p = {"key": "aiohttp", "name": "aiohttp", "from": "3.14.3", "to": "3.14.4", "ids": "X-1",
             "wheels": va.compatible_wheels([wheel("aiohttp", "3.14.4", 16)]),
             "old_wheels": va.compatible_wheels([wheel("aiohttp", "3.14.3", 99)]), "deps": deps}
        up, down = va.req_lines([p], "to"), va.req_lines([p], "from")
        self.assertEqual([l.split()[0] for l in up.splitlines()], ["aiohttp==3.14.4", "yarl==1.25.1"])
        self.assertEqual([l.split()[0] for l in down.splitlines()], ["aiohttp==3.14.3", "yarl==1.24.5"])
        self.assertEqual(va.names_of([p]), "aiohttp 3.14.3 -> 3.14.4 (with yarl 1.24.5 -> 1.25.1)")

    def test_one_version_of_each_package_per_batch(self):
        def pick(name, to, deps=()):
            return {"key": name, "name": name, "from": "0", "to": to, "deps": [
                {"key": n, "name": n, "from": "0", "to": t} for n, t in deps]}
        a = pick("aiohttp", "3.14.4", [("yarl", "1.25.1")])
        same = pick("yarl", "1.25.1")                     # yarl's own fix, at the version aiohttp needs
        other = pick("yarl", "1.26.0")
        b = pick("b", "2.0", [("yarl", "1.26.0")])
        kept, waiting = va.one_version_each([a, same, other, b])
        self.assertEqual([p["name"] for p in kept], ["aiohttp", "yarl"])
        self.assertEqual(len(waiting), 2)
        self.assertIn("waits for the next run", waiting[0])
        self.assertEqual([m["name"] for m in va.members(kept)], ["aiohttp", "yarl"])   # yarl once
        with self.assertRaises(va.Fail):
            va.members([a, other])

    def test_plan_wires_it_in(self):
        src = (HERE / "venv-autopatch.py").read_text()
        self.assertIn('deps, why = dependency_upgrades(cfg, v, led, live, d["name"], ver, now)', src)
        self.assertIn('"wheels": wheels, "old_wheels": old_wheels, "deps": deps})', src)
        dedup = src.index("picks, waiting = one_version_each(ready)")
        audit = src.index('clean = audit(cfg, {m["name"]: m["to"] for p in picks for m in members([p])})')
        self.assertLess(dedup, audit, "the audit must see one version of each package")


# The memory venv's opentelemetry family on 2026-10-25, Requires-Dist as installed and as PyPI records
# them (2026-10-26). api 1.45.1 fixes GHSA-xcq7-p3pf-p88h; sdk and semantic-conventions pin api exactly.
OTEL_INSTALLED = [
    ("opentelemetry-api", "1.45.0", []),
    ("opentelemetry-sdk", "1.45.0", ["opentelemetry-api==1.45.0", "opentelemetry-semantic-conventions==0.66b0",
                                     "opentelemetry-configuration==0.66b0; extra == 'file-configuration'"]),
    ("opentelemetry-semantic-conventions", "0.66b0", ["opentelemetry-api==1.45.0"]),
    ("opentelemetry-exporter-otlp-common", "0.66b0", ["opentelemetry-sdk~=1.45.0"]),
    ("opentelemetry-exporter-otlp-proto-grpc", "1.45.0", [
        "opentelemetry-api~=1.15", "opentelemetry-exporter-otlp-common==0.66b0", "opentelemetry-sdk~=1.45.0",
        "opentelemetry-exporter-credential-provider-gcp>=0.59b0; extra == 'gcp-auth'"]),
]
OTEL_PYPI = {("opentelemetry-api", "1.45.1"): [], ("opentelemetry-api", "1.45.2"): [],
             ("opentelemetry-sdk", "1.45.1"): ["opentelemetry-api==1.45.1", "opentelemetry-semantic-conventions==0.66b1"],
             ("opentelemetry-sdk", "1.45.2"): ["opentelemetry-api==1.45.2", "opentelemetry-semantic-conventions==0.66b2"],
             ("opentelemetry-semantic-conventions", "0.66b1"): ["opentelemetry-api==1.45.1"],
             ("opentelemetry-semantic-conventions", "0.66b2"): ["opentelemetry-api==1.45.2"]}


class SiblingPins(unittest.TestCase):
    """opentelemetry-api 1.45.1 failed its rehearsal on 2026-10-25: opentelemetry-sdk and
    -semantic-conventions 1.45.0/0.66b0 pin api==1.45.0, and nothing moved them. A package that pins one the
    batch moves now moves with it, as its oldest release that accepts the batch."""

    def setUp(self):
        self.cfg = {"cooldown_days": 7}
        self.v = {"holds": {}}
        self.led = {"failed": {}, "user_managed": {}}
        self.live = {va.canonicalize_name(n): dict(pkg(n, ver), requires=list(req)) for n, ver, req in OTEL_INSTALLED}
        self.requires = dict(OTEL_PYPI)
        self.releases = {
            "opentelemetry-sdk": {"1.45.0": [wheel("opentelemetry_sdk", "1.45.0", 31)],
                                  "1.45.1": [wheel("opentelemetry_sdk", "1.45.1", 20)],
                                  "1.45.2": [wheel("opentelemetry_sdk", "1.45.2", 10)],
                                  "1.46.0.dev1": [wheel("opentelemetry_sdk", "1.46.0.dev1", 9)]},
            "opentelemetry-semantic-conventions": {
                "0.66b0": [wheel("opentelemetry_semantic_conventions", "0.66b0", 31)],
                "0.66b1": [wheel("opentelemetry_semantic_conventions", "0.66b1", 20)],
                "0.66b2": [wheel("opentelemetry_semantic_conventions", "0.66b2", 10)]}}

    def deps(self, ver="1.45.1"):
        return va.dependency_upgrades(self.cfg, self.v, self.led, self.live, "opentelemetry-api", ver, NOW,
                                      requires_of=lambda n, v: self.requires[(n, v)],
                                      releases_of=lambda n: self.releases[n])

    def test_the_memory_case_carries_sdk_and_semantic_conventions(self):
        deps, why = self.deps()
        self.assertEqual(why, "")
        self.assertEqual([(d["name"], d["from"], d["to"], d["for"]) for d in deps], [
            ("opentelemetry-sdk", "1.45.0", "1.45.1", "opentelemetry-api 1.45.1"),
            ("opentelemetry-semantic-conventions", "0.66b0", "0.66b1", "opentelemetry-sdk 1.45.1")])
        # The exporters' ~=1.45.0 accepts sdk 1.45.1, so they stay; the extras' pins never count.
        self.assertEqual(va.pins_against(self.live, {"opentelemetry-api": "1.45.1", "opentelemetry-sdk": "1.45.1",
                                                     "opentelemetry-semantic-conventions": "0.66b1"}), [])

    def test_the_oldest_release_that_accepts_the_batch(self):
        deps, why = self.deps("1.45.2")          # 1.45.1 of each pins api 1.45.1, so 1.45.2 of each
        self.assertEqual(why, "")
        self.assertEqual([(d["name"], d["to"]) for d in deps], [("opentelemetry-sdk", "1.45.2"),
                                                               ("opentelemetry-semantic-conventions", "0.66b2")])
        # Two releases accept api 1.45.1: the older one moves.
        self.releases["opentelemetry-sdk"]["1.45.3"] = [wheel("opentelemetry_sdk", "1.45.3", 8)]
        self.requires[("opentelemetry-sdk", "1.45.3")] = ["opentelemetry-api>=1.45.1",
                                                          "opentelemetry-semantic-conventions==0.66b1"]
        self.assertEqual(self.deps()[0][0]["to"], "1.45.1")

    def test_what_is_reported_instead(self):
        sdk = "opentelemetry-sdk"
        cases = [
            (lambda: self.v["holds"].__setitem__(sdk, "pinned by hand"), "held in the config: pinned by hand"),
            (lambda: self.led["user_managed"].__setitem__(sdk, "changed outside this tool"), "user-managed"),
            (lambda: self.live.__setitem__(sdk, dict(self.live[sdk], from_pypi=False, why="installed from file:///s")),
             "opentelemetry-sdk is installed from file:///s"),
            (lambda: self.releases[sdk].__setitem__("1.45.1", [wheel("opentelemetry_sdk", "1.45.1", 3)]),
             "opentelemetry-sdk 1.45.0 pins opentelemetry-api==1.45.0, which this fix moves to 1.45.1"),
            (lambda: self.requires.__setitem__((sdk, "1.45.1"), ["opentelemetry-api==1.45.2"]), "needs opentelemetry-api"),
            (lambda: self.releases[sdk].__setitem__("1.45.0", [sdist("opentelemetry_sdk", "1.45.0", 31)]), "roll back"),
            (lambda: self.requires.__setitem__((sdk, "1.45.1"), ["opentelemetry-api==1.45.1 ; nonsense ="]),
             "could not read opentelemetry-sdk's requirements"),
            (lambda: self.live.__setitem__("pinner", dict(pkg("pinner", "2.0"), requires=["opentelemetry-api<1.45.1"])),
             "pinner 2.0 pins opentelemetry-api<1.45.1"),        # no release of pinner on PyPI
        ]
        for change, why in cases:
            self.setUp()
            self.releases.setdefault("pinner", {"2.0": [wheel("pinner", "2.0", 99)]})
            change()
            deps, got = self.deps()
            self.assertIsNone(deps, why)
            self.assertIn(why, got)

    def test_a_pin_the_batch_does_not_touch_is_left_to_pip_check(self):
        # Already broken before the fix (pip check's baseline), or unreadable: not this fix's business.
        self.live["other"] = dict(pkg("other", "1.0"), requires=["idna<2", "bad line ;;; ="])
        self.assertEqual(va.pins_against(self.live, {"opentelemetry-api": "1.45.0"}), [])
        self.assertEqual(self.deps()[1], "")
        # A pin behind an extra doesn't count (pip check leaves extras out); a beta the batch moves to
        # satisfies a range that names no pre-release.
        self.live["other"]["requires"] = ["opentelemetry-api==1.45.0; extra == 'otel'",
                                          "opentelemetry-semantic-conventions<1"]
        self.assertEqual(va.pins_against({"other": self.live["other"]}, {"opentelemetry-api": "1.45.1",
                                                                         "opentelemetry-semantic-conventions": "0.66b1"}), [])

    def test_a_carry_that_needs_a_carry(self):
        # sdk 1.45.1 moves; the exporter pins sdk exactly, so it moves too; grpc pins the exporter exactly
        # (as installed), so grpc moves after it.
        exp, grpc = "opentelemetry-exporter-otlp-common", "opentelemetry-exporter-otlp-proto-grpc"
        self.live[exp]["requires"] = ["opentelemetry-sdk==1.45.0"]
        self.releases[exp] = {"0.66b0": [wheel("x", "0.66b0", 31)], "0.66b1": [wheel("x", "0.66b1", 20)]}
        self.requires[(exp, "0.66b1")] = ["opentelemetry-sdk==1.45.1"]
        self.releases[grpc] = {"1.45.0": [wheel("g", "1.45.0", 31)], "1.45.1": [wheel("g", "1.45.1", 20)]}
        self.requires[(grpc, "1.45.1")] = ["opentelemetry-api~=1.15", "opentelemetry-exporter-otlp-common==0.66b1",
                                           "opentelemetry-sdk~=1.45.1"]
        deps, why = self.deps()
        self.assertEqual(why, "")
        self.assertEqual([(d["name"], d["for"]) for d in deps], [
            ("opentelemetry-sdk", "opentelemetry-api 1.45.1"),
            ("opentelemetry-semantic-conventions", "opentelemetry-sdk 1.45.1"),
            (exp, "opentelemetry-sdk 1.45.1"), (grpc, f"{exp} 0.66b1")])

    def test_each_release_is_read_once(self):
        calls = []
        va.dependency_upgrades(self.cfg, self.v, self.led, self.live, "opentelemetry-api", "1.45.1", NOW,
                               requires_of=lambda n, v: calls.append((n, v)) or self.requires[(n, v)],
                               releases_of=lambda n: self.releases[n])
        self.assertEqual(len(calls), len(set(calls)), calls)   # sdk 1.45.1 was read by the carry and the queue

    def test_a_members_bound_against_a_later_move_is_reported(self):
        # 2026-10-26: x 2.0 needs y<2.9 (met when x is queued); w pins x==1.0, so w moves to 1.1,
        # which needs y>=2.9, so y moves. x 2.0's bound is broken by then: a plan line, not a rehearsal strike.
        live = {"x": dict(pkg("x", "1.0"), requires=["y<2.9"]), "y": pkg("y", "2.5"),
                "w": dict(pkg("w", "1.0"), requires=["x==1.0"])}
        req = {("x", "2.0"): ["y<2.9"], ("w", "1.1"): ["x>=2.0", "y>=2.9"], ("y", "2.9"): []}
        rels = {n: {ver: [wheel(n, ver, 30)] for ver in vs} for n, vs in
                {"x": ("1.0", "2.0"), "y": ("2.5", "2.9"), "w": ("1.0", "1.1")}.items()}
        deps, why = va.dependency_upgrades(self.cfg, self.v, self.led, live, "x", "2.0", NOW,
                                           requires_of=lambda n, v: req[(n, v)], releases_of=lambda n: rels[n])
        self.assertIsNone(deps)
        self.assertEqual(why, "x 2.0 needs y<2.9, but this fix moves y to 2.9")

    def test_two_advisories_in_one_family_are_not_stuck(self):
        # 2026-10-26: advisory A on api < 1.45.1, B on sdk < 1.45.2. A's carry takes sdk to 1.45.1,
        # B (sdk to 1.45.2) waits for A, the audit drops A for its carry's own advisory, and every week neither
        # applies. A dropped pick frees what it moved, so B is planned against the batch again, and fixes both.
        canon = va.canonicalize_name
        rels = {n: {ver: [wheel(n.replace("-", "_"), ver, 30)] for ver in vs} for n, vs in {
            "opentelemetry-api": ("1.45.0", "1.45.1", "1.45.2"), "opentelemetry-sdk": ("1.45.0", "1.45.1", "1.45.2"),
            "opentelemetry-semantic-conventions": ("0.66b0", "0.66b1", "0.66b2")}.items()}
        live = {k: v for k, v in self.live.items() if k in rels}

        def audit(cfg, pins):
            out = {}
            for n, ver in pins.items():
                if canon(n) == "opentelemetry-api" and va.Version(ver) < va.Version("1.45.1"):
                    out[canon(n)] = [{"id": "GHSA-A", "fix_versions": ["1.45.1"]}]
                if canon(n) == "opentelemetry-sdk" and va.Version(ver) < va.Version("1.45.2"):
                    out[canon(n)] = [{"id": "GHSA-B", "fix_versions": ["1.45.2"]}]
            return out
        stubs = {"check_interpreter": lambda p: None, "inspect_venv": lambda c, p: live, "audit": audit,
                 "load_ledger": lambda c, n: {"freeze": None, "user_managed": {}, "failed": {}, "history": []},
                 "pypi_releases": lambda n: rels[n], "pypi_requires": lambda n, v: self.requires[(n, v)]}
        for k, f in stubs.items():
            self.addCleanup(setattr, va, k, getattr(va, k))
            setattr(va, k, f)
        pl = va.plan_venv({"cooldown_days": 7, "prefer": "oldest"}, {"name": "memory", "path": "/x", "holds": {}}, NOW)
        self.assertEqual(va.names_of(pl["picks"]), "opentelemetry-sdk 1.45.0 -> 1.45.2 (with opentelemetry-api 1.45.0 "
                         "-> 1.45.2, opentelemetry-semantic-conventions 0.66b0 -> 0.66b2)")
        self.assertTrue(any("has advisories of its own (GHSA-B)" in r for r in pl["report"]), pl["report"])
        self.assertFalse(any("waits for the next run" in r for r in pl["report"]), pl["report"])

    def test_choose_carry_reads_at_most_ten_releases(self):
        rels = {f"1.{i}": [wheel("p", f"1.{i}", 30)] for i in range(30)}
        read = []
        ver, why = va.choose_carry("1.0", rels, lambda to: read.append(to) or va.Requirement("x<0"), NOW, 7, {})
        self.assertIsNone(ver)
        self.assertEqual((len(read), read[0]), (va.MAX_DEPENDENCY_UPGRADES, "1.1"))
        self.assertIn("none of the 10 oldest", why)

    def test_the_inspect_snippet_returns_requires(self):
        with tempfile.TemporaryDirectory() as d:
            di = Path(d, "opentelemetry_sdk-1.45.0.dist-info")
            di.mkdir()
            (di / "METADATA").write_text("Name: opentelemetry-sdk\nVersion: 1.45.0\n"
                                         "Requires-Dist: opentelemetry-api==1.45.0\n")
            p = subprocess.run([sys.executable, "-I", "-S", "-c", va.INSPECT, d], capture_output=True, text=True)
            got = __import__("json").loads(p.stdout)
            self.assertEqual(got[0]["requires"], ["opentelemetry-api==1.45.0"])


class PreReleases(unittest.TestCase):
    """A pre-release only for a package whose installed version is one; never a dev build."""

    def test_only_when_the_installed_version_is_one(self):
        r = {"2.0": [wheel("p", "2.0", 99)], "2.1b1": [wheel("p", "2.1b1", 30)], "2.1": [wheel("p", "2.1", 3)]}
        spec = va.SpecifierSet(">=2.1b1")
        self.assertIsNone(va.choose_dependency("2.0", spec, r, NOW, 7, {})[0])
        r["2.0b3"] = [wheel("p", "2.0b3", 99)]
        self.assertEqual(va.choose_dependency("2.0b3", spec, r, NOW, 7, {})[0], "2.1b1")
        self.assertEqual(va.choose("2.0b3", [vuln("A", "2.1b1")], r, NOW, 7, {})[0], "2.1b1")
        self.assertIsNone(va.choose("2.0", [vuln("A", "2.1b1")], r, NOW, 7, {})[0])

    def test_never_a_dev_or_local_build(self):
        r = {"0.66b0": [wheel("s", "0.66b0", 99)], "0.66b1.dev2": [wheel("s", "0.66b1.dev2", 30)]}
        self.assertIsNone(va.choose_dependency("0.66b0", va.SpecifierSet(">0.66b0"), r, NOW, 7, {})[0])
        self.assertFalse(va.candidate(va.Version("1.0+cpu"), True))


class Strikes(unittest.TestCase):
    def test_an_applied_version_loses_its_strikes(self):
        led = {"rehearsal_failures": {"opentelemetry-api": {"1.45.1": 1}, "idna": {"3.7": 1}},
               "live_failures": {"opentelemetry-api": {"1.45.1": 1, "1.45.2": 1}}}
        p = {"key": "opentelemetry-api", "name": "opentelemetry-api", "from": "1.45.0", "to": "1.45.1",
             "deps": [{"key": "idna", "name": "idna", "from": "3.6", "to": "3.8"}]}
        va.clear_strikes(led, [p])
        self.assertEqual(led, {"rehearsal_failures": {"idna": {"3.7": 1}},
                               "live_failures": {"opentelemetry-api": {"1.45.2": 1}}})

    def test_patch_wires_it_in(self):
        src = (HERE / "venv-autopatch.py").read_text()
        self.assertIn('report.append(f"patched {names}")\n        clear_strikes(led, picks)', src)
        self.assertIn('pl.setdefault("struck", []).append(p["name"])', src)


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

    def test_a_strike_reaches_the_operator_whatever_the_outcome(self):
        # 2026-10-25: opentelemetry-api struck, filelock deferred, the subject read "weekly report".
        struck = ["opentelemetry-api"]
        for outcome in ("deferred", "report only", "patched"):
            self.assertTrue(va.needs_attention(outcome, None, struck), outcome)
        self.assertEqual(va.attention_line("memory", "deferred", None, struck),
                         "memory deferred, opentelemetry-api not applied")
        self.assertEqual(va.attention_line("memory", "deferred", "deferred"), "memory deferred twice running")
        self.assertEqual(va.attention_line("cad", "not applied", None, ["aiohttp"]), "cad not applied")

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
        self.assertIn("if apply and needs_attention(outcome, previous, struck):", src)
        self.assertIn('attention.append(attention_line(v["name"], outcome, previous, struck))', src)
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
