import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bump


def write(path, content):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "wb") as f:
        f.write(content if isinstance(content, bytes)
                else content.encode("utf-8"))


def conf_text(builder="2.2.5", anyvm="0.5.4",
              sync='"rsync,scp,sshfs,nfs"',
              shutdown='"/sbin/shutdown -p now"', crlf=False):
    eol = "\r\n" if crlf else "\n"
    return eol.join([
        "BUILDER_VERSION=%s" % builder,
        "",
        "ANYVM_VERSION=%s" % anyvm,
        "",
        "VM_SYNC_METHODS=%s" % sync,
        "",
        "VM_SHUTDOWN_CMD=%s" % shutdown,
    ]) + eol


class Fetch(object):
    """Injectable fetcher: maps url substrings to bytes or an Exception.
    Records (url, method); a HEAD hit returns b"" like the real fetcher."""

    def __init__(self, table):
        self.table = table
        self.seen = []

    def __call__(self, url, method="GET"):
        self.seen.append((url, method))
        for key, val in self.table.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return b"" if method == "HEAD" else val
        raise bump.FetchError("HTTP 404")


class BumpCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(tmp.name)
        os.makedirs("conf")
        write(os.path.join(".github", "data", "datafile.ini"),
              "VM_NAME=Demo\nVM_OS_NAME=demo\n")

    def add(self, name, content):
        write(os.path.join("conf", name), content)


class TestNaturalKey(unittest.TestCase):
    def test_numeric_ordering(self):
        ks = bump.natural_key
        self.assertLess(ks("2.2.5"), ks("2.2.10"))
        self.assertLess(ks("2.9.9"), ks("2.10.0"))
        self.assertLess(ks("2.2.5"), ks("3.0.0"))

    def test_v_prefix_is_stripped_by_caller_semantics(self):
        self.assertEqual(bump.strip_v("v2.2.5"), "2.2.5")
        self.assertEqual(bump.strip_v("2.2.5"), "2.2.5")


class TestScanConfs(BumpCase):
    def test_reads_pins_from_all_confs(self):
        self.add("15.1.conf", conf_text())
        self.add("15.1-aarch64.conf", conf_text())
        write("conf/default.release.conf", "DEFAULT_RELEASE=15.1\n")
        write("conf/test.releases", '"15.0", "15.1"\n')
        confs = bump.scan_confs()
        self.assertEqual(sorted(c["name"] for c in confs),
                         ["15.1-aarch64.conf", "15.1.conf"])
        self.assertEqual(confs[0]["builder"], "2.2.5")
        self.assertEqual(confs[0]["anyvm"], "0.5.4")

    def test_mixed_anyvm_versions_refused(self):
        self.add("15.0.conf", conf_text(anyvm="0.5.3"))
        self.add("15.1.conf", conf_text(anyvm="0.5.4"))
        confs = bump.scan_confs()
        with self.assertRaises(bump.MixedPins):
            bump.lockstep_value(confs, "anyvm")

    def test_lockstep_value(self):
        self.add("15.0.conf", conf_text())
        self.add("15.1.conf", conf_text())
        confs = bump.scan_confs()
        self.assertEqual(bump.lockstep_value(confs, "anyvm"), "0.5.4")

    def test_tag_is_the_conf_basename(self):
        self.add("15.1-aarch64.conf", conf_text())
        confs = bump.scan_confs()
        self.assertEqual(confs[0]["tag"], "15.1-aarch64")


class TestRewrite(BumpCase):
    def test_line_scoped_and_eol_preserving(self):
        self.add("a.conf", conf_text(crlf=True))
        self.add("b.conf", conf_text(crlf=False))
        n = bump.rewrite_key("BUILDER_VERSION", "2.2.6",
                             only=["a.conf", "b.conf"])
        self.assertEqual(n, 2)
        a = open("conf/a.conf", "rb").read()
        b = open("conf/b.conf", "rb").read()
        self.assertIn(b"BUILDER_VERSION=2.2.6\r\n", a)
        self.assertNotIn(b"\r\r", a)
        self.assertIn(b"BUILDER_VERSION=2.2.6\n", b)
        self.assertNotIn(b"\r", b)
        # untouched line survives byte-identically
        self.assertIn(b'VM_SYNC_METHODS="rsync,scp,sshfs,nfs"', b)

    def test_only_the_named_key_changes(self):
        self.add("a.conf", conf_text())
        bump.rewrite_key("BUILDER_VERSION", "9.9.9")
        data = open("conf/a.conf", "rb").read()
        self.assertIn(b"ANYVM_VERSION=0.5.4", data)
        self.assertIn(b"BUILDER_VERSION=9.9.9", data)

    def test_only_listed_confs_are_touched(self):
        self.add("new.conf", conf_text(builder="2.0.9"))
        self.add("legacy.conf", conf_text(builder="1.1.4"))
        n = bump.rewrite_key("BUILDER_VERSION", "2.1.0", only=["new.conf"])
        self.assertEqual(n, 1)
        self.assertIn(b"BUILDER_VERSION=2.1.0",
                      open("conf/new.conf", "rb").read())
        self.assertIn(b"BUILDER_VERSION=1.1.4",
                      open("conf/legacy.conf", "rb").read())


def rel(tag, release=None, arch="x86_64", sync="rsync,scp",
        shutdown="/sbin/shutdown -p now", desktop=False, build=True):
    return {"tag": tag, "release": release or tag, "arch": arch,
            "sync": sync, "shutdown": shutdown,
            "desktop": desktop, "build": build}


class TestPlanNewConfs(BumpCase):
    def test_new_release_gets_conf_from_newest_same_arch(self):
        self.add("15.0.conf", conf_text())
        self.add("15.1.conf", conf_text())
        self.add("15.1-aarch64.conf", conf_text(sync='"nfs,scp"'))
        idx = [rel("15.1"), rel("15.1", arch="aarch64"),
               rel("15.2", sync="rsync,scp,sshfs,nfs"),
               rel("15.2", arch="aarch64", sync="nfs,scp")]
        plan = bump.plan_new_confs(idx, "2.2.6")
        names = sorted(p["name"] for p in plan)
        self.assertEqual(names, ["15.2-aarch64.conf", "15.2.conf"])
        base = [p for p in plan if p["name"] == "15.2.conf"][0]
        self.assertIn("BUILDER_VERSION=2.2.6", base["content"])
        self.assertIn('VM_SYNC_METHODS="rsync,scp,sshfs,nfs"',
                      base["content"])
        arm = [p for p in plan if p["name"] == "15.2-aarch64.conf"][0]
        self.assertIn('VM_SYNC_METHODS="nfs,scp"', arm["content"])

    def test_desktop_skipped_when_repo_carries_none(self):
        # freebsd-vm's shape: the builder ships xfce/gnome images but the
        # action never carried them
        self.add("15.1.conf", conf_text())
        idx = [rel("15.1"),
               rel("15.2-xfce", desktop=True),
               rel("15.2-old", build=False),
               rel("15.2")]
        plan = bump.plan_new_confs(idx, "2.2.6")
        self.assertEqual([p["name"] for p in plan], ["15.2.conf"])

    def test_desktop_created_when_repo_already_carries_one(self):
        # ghostbsd-vm's shape: 26.1-xfce/-gershwin ARE desktop:true in the
        # builder index and the action carries them
        self.add("26.1.conf", conf_text())
        self.add("26.1-xfce.conf", conf_text())
        idx = [rel("26.1"), rel("26.1-xfce", desktop=True),
               rel("26.2"), rel("26.2-xfce", desktop=True)]
        plan = bump.plan_new_confs(idx, "2.0.7")
        self.assertEqual(sorted(p["name"] for p in plan),
                         ["26.2-xfce.conf", "26.2.conf"])
        xfce = [p for p in plan if p["name"] == "26.2-xfce.conf"][0]
        self.assertEqual(xfce["template"], "26.1-xfce.conf")

    def test_base_template_is_preferred_over_variants(self):
        # a filename sort once ranked 26.1-xfce.conf above 26.1.conf
        # because ".conf" participated in the comparison
        self.add("26.1.conf", conf_text())
        self.add("26.1-xfce.conf",
                 conf_text(sync='"scp"'))
        idx = [rel("26.1"), rel("26.1-xfce"), rel("26.2")]
        plan = bump.plan_new_confs(idx, "2.0.7")
        base = [p for p in plan if p["name"] == "26.2.conf"][0]
        self.assertEqual(base["template"], "26.1.conf")

    def test_variant_template_prefers_same_suffix(self):
        self.add("r151058.conf", conf_text())
        self.add("r151058-build.conf", conf_text(sync='"rsync,scp"'))
        idx = [rel("r151058"), rel("r151058-build"),
               rel("r151060"), rel("r151060-build")]
        plan = bump.plan_new_confs(idx, "2.1.3")
        build = [p for p in plan if p["name"] == "r151060-build.conf"][0]
        self.assertEqual(build["template"], "r151058-build.conf")

    def test_seed_appends_missing_fields(self):
        # openbsd's 7.2-era confs lack VM_SYNC_METHODS entirely; a new
        # conf copied from one must still get the field
        self.add("7.9.conf",
                 "BUILDER_VERSION=2.0.9\nANYVM_VERSION=0.5.4\n")
        idx = [rel("7.9"), rel("8.0", sync="rsync,scp,nfs")]
        plan = bump.plan_new_confs(idx, "2.1.0")
        base = [p for p in plan if p["name"] == "8.0.conf"][0]
        self.assertIn('VM_SYNC_METHODS="rsync,scp,nfs"', base["content"])
        self.assertIn('VM_SHUTDOWN_CMD=', base["content"])

    def test_build_variant_is_included(self):
        self.add("r1.conf", conf_text())
        idx = [rel("r1"), rel("r2"), rel("r2-build")]
        plan = bump.plan_new_confs(idx, "2.2.6")
        self.assertEqual(sorted(p["name"] for p in plan),
                         ["r2-build.conf", "r2.conf"])

    def test_existing_conf_is_not_recreated(self):
        self.add("15.1.conf", conf_text())
        idx = [rel("15.1")]
        self.assertEqual(bump.plan_new_confs(idx, "2.2.6"), [])


class TestTestReleases(BumpCase):
    def test_appends_new_releases_once(self):
        write("conf/test.releases", '"15.0", "15.1"\n')
        out = bump.append_test_releases(["15.2", "15.2"])
        self.assertTrue(out)
        data = open("conf/test.releases", "r").read()
        self.assertEqual(data, '"15.0", "15.1", "15.2"\n')

    def test_arch_variants_never_appended(self):
        write("conf/test.releases", '"15.1"\n')
        # caller passes release VALUES, not tags -- an arch tag must have
        # been reduced to its release before this point; passing an
        # already-present value is a no-op
        self.assertFalse(bump.append_test_releases(["15.1"]))
        self.assertEqual(open("conf/test.releases").read(), '"15.1"\n')


class TestGates(BumpCase):
    def test_missing_release_index_fails(self):
        fetch = Fetch({})
        with self.assertRaises(bump.FetchError):
            bump.fetch_release_index("demo", "2.2.6", fetch)

    def test_index_parsed(self):
        fetch = Fetch({"v2.2.6/releases.json":
                       json.dumps({"os": "demo",
                                   "releases": [rel("15.1")]}).encode()})
        idx = bump.fetch_release_index("demo", "2.2.6", fetch)
        self.assertEqual(idx[0]["tag"], "15.1")

    def test_image_check_uses_head_never_get(self):
        # images are >1 GB; a GET-based existence check would transfer
        # tens of GB per nightly run and read each body into RAM
        seen = []

        def fetch(url, method="GET"):
            seen.append((url, method))
            if "demo-15.1.qcow2.zst" in url:
                return b""
            raise bump.FetchError("HTTP 404")

        self.assertTrue(bump.image_exists("demo", "2.2.6", "15.1", fetch))
        self.assertFalse(bump.image_exists("demo", "2.2.6", "9.9", fetch))
        self.assertTrue(all(m == "HEAD" for _, m in seen))


class TestMain(BumpCase):
    def _index(self, entries):
        return json.dumps({"os": "demo", "releases": entries}).encode()

    def _latest(self, tag):
        return json.dumps({"tag_name": tag}).encode()

    def test_noop_when_not_newer(self):
        self.add("15.1.conf", conf_text(builder="2.2.5"))
        fetch = Fetch({"releases/latest": self._latest("v2.2.5")})
        rc = bump.main(["--check"], fetch=fetch)
        self.assertEqual(rc, 0)

    def test_full_bump_with_new_release(self):
        self.add("15.0.conf", conf_text())
        self.add("15.1.conf", conf_text())
        write("conf/test.releases", '"15.0", "15.1"\n')
        write("conf/default.release.conf", "DEFAULT_RELEASE=15.1\n")
        fetch = Fetch({
            "releases/latest": self._latest("v2.2.6"),
            "v2.2.6/releases.json": self._index(
                [rel("15.0"), rel("15.1"), rel("15.2")]),
            "demo-15.0.qcow2.zst": b"ok",
            "demo-15.1.qcow2.zst": b"ok",
            "demo-15.2.qcow2.zst": b"ok",
        })
        rc = bump.main([], fetch=fetch)
        self.assertEqual(rc, 0)
        self.assertIn(b"BUILDER_VERSION=2.2.6",
                      open("conf/15.1.conf", "rb").read())
        self.assertTrue(os.path.exists("conf/15.2.conf"))
        self.assertEqual(open("conf/test.releases").read(),
                         '"15.0", "15.1", "15.2"\n')
        # default release untouched (user decision 3)
        self.assertEqual(open("conf/default.release.conf").read(),
                         "DEFAULT_RELEASE=15.1\n")

    def test_check_mode_writes_nothing(self):
        self.add("15.1.conf", conf_text())
        write("conf/test.releases", '"15.1"\n')
        fetch = Fetch({
            "releases/latest": self._latest("v2.2.6"),
            "v2.2.6/releases.json": self._index([rel("15.1"), rel("15.2")]),
            "demo-15.1.qcow2.zst": b"ok",
            "demo-15.2.qcow2.zst": b"ok",
        })
        rc = bump.main(["--check"], fetch=fetch)
        self.assertEqual(rc, 0)
        self.assertIn(b"BUILDER_VERSION=2.2.5",
                      open("conf/15.1.conf", "rb").read())
        self.assertFalse(os.path.exists("conf/15.2.conf"))

    def test_legacy_conf_absent_from_index_keeps_its_pin(self):
        # openbsd's real shape: 7.2 images exist only at an old builder
        # tag; the new index does not list 7.2, so its conf must not move
        self.add("7.2.conf", conf_text(builder="0.9.9"))
        self.add("7.9.conf", conf_text(builder="2.0.9"))
        fetch = Fetch({
            "releases/latest": self._latest("v2.1.0"),
            "v2.1.0/releases.json": self._index([rel("7.9"), rel("8.0")]),
            "demo-7.9.qcow2.zst": b"ok",
            "demo-8.0.qcow2.zst": b"ok",
        })
        rc = bump.main([], fetch=fetch)
        self.assertEqual(rc, 0)
        self.assertIn(b"BUILDER_VERSION=0.9.9",
                      open("conf/7.2.conf", "rb").read())
        self.assertIn(b"BUILDER_VERSION=2.1.0",
                      open("conf/7.9.conf", "rb").read())
        self.assertTrue(os.path.exists("conf/8.0.conf"))

    def test_any_bumped_conf_missing_image_fails_all(self):
        self.add("15.0.conf", conf_text())
        self.add("15.1.conf", conf_text())
        fetch = Fetch({
            "releases/latest": self._latest("v2.2.6"),
            "v2.2.6/releases.json": self._index([rel("15.0"), rel("15.1")]),
            "demo-15.1.qcow2.zst": b"ok",
            # demo-15.0.qcow2.zst missing -- half-built tag
        })
        rc = bump.main([], fetch=fetch)
        self.assertEqual(rc, 1)
        self.assertIn(b"BUILDER_VERSION=2.2.5",
                      open("conf/15.1.conf", "rb").read())

    def test_mixed_anyvm_pins_refuse_before_any_write(self):
        self.add("15.0.conf", conf_text(anyvm="0.5.3"))
        self.add("15.1.conf", conf_text(anyvm="0.5.4"))
        fetch = Fetch({"releases/latest": self._latest("v2.2.5")})
        rc = bump.main([], fetch=fetch)
        self.assertEqual(rc, 1)
        self.assertIn(b"ANYVM_VERSION=0.5.3",
                      open("conf/15.0.conf", "rb").read())

    def test_index_missing_fails_red(self):
        self.add("15.1.conf", conf_text())
        fetch = Fetch({"releases/latest": self._latest("v2.2.6")})
        rc = bump.main([], fetch=fetch)
        self.assertEqual(rc, 1)
        self.assertIn(b"BUILDER_VERSION=2.2.5",
                      open("conf/15.1.conf", "rb").read())

    def test_image_missing_fails_red(self):
        self.add("15.1.conf", conf_text())
        fetch = Fetch({
            "releases/latest": self._latest("v2.2.6"),
            "v2.2.6/releases.json": self._index([rel("15.1"), rel("15.2")]),
        })
        rc = bump.main([], fetch=fetch)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists("conf/15.2.conf"))

    def test_anyvm_lockstep_follow_with_asset(self):
        self.add("15.1.conf", conf_text(anyvm="0.5.4"))
        fetch = Fetch({
            "builder/releases/latest": self._latest("v2.2.5"),
            "anyvm/releases/latest": self._latest("v0.5.5"),
            "v0.5.5/anyvm.py": b"#!python",
        })
        rc = bump.main([], fetch=fetch)
        self.assertEqual(rc, 0)
        self.assertIn(b"ANYVM_VERSION=0.5.5",
                      open("conf/15.1.conf", "rb").read())

    def test_anyvm_without_asset_is_not_followed(self):
        self.add("15.1.conf", conf_text(anyvm="0.5.4"))
        fetch = Fetch({
            "builder/releases/latest": self._latest("v2.2.5"),
            "anyvm/releases/latest": self._latest("v0.5.5"),
        })
        rc = bump.main([], fetch=fetch)
        # not an error: the release exists but is not consumable yet;
        # warn and wait for the asset to appear
        self.assertEqual(rc, 0)
        self.assertIn(b"ANYVM_VERSION=0.5.4",
                      open("conf/15.1.conf", "rb").read())


if __name__ == "__main__":
    unittest.main()
