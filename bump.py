#!/usr/bin/env python3
# bump.py -- propagate a human-cut anyvm-org/<os>-builder release into this
# *-vm sibling: bump BUILDER_VERSION in every conf, create confs for new
# releases the builder now ships, extend conf/test.releases, and follow
# anyvm releases (ANYVM_VERSION) when they carry the anyvm.py asset.
#
# Run from a sibling repo root, with base-vm cloned alongside:
#   python3 base-vm/bump.py [--check]
#
# --check prints what it would do and never touches the disk.
#
# Design: docs/superpowers/specs/2026-07-30-version-bump-bots-design.md in
# the anyvm-org tree. The bot reacts to releases; it never creates one.
# DEFAULT_RELEASE, sync-map.json and exclude-missing.txt are never touched
# (the first is a human call, the others are CI-baked from the conf set).

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

CONF_DIR = "conf"
DATAFILE = os.path.join(".github", "data", "datafile.ini")
BUILDER_ORG = "anyvm-org"
API = "https://api.github.com"
ASSIGN_RE = re.compile(rb"^(BUILDER_VERSION|ANYVM_VERSION)=([^\r\n]*)(\r?\n?)$")
KEYMAP = {"BUILDER_VERSION": "builder", "ANYVM_VERSION": "anyvm"}


class MixedPins(Exception):
    pass


class FetchError(Exception):
    pass


def log(msg):
    sys.stdout.write("bump: %s\n" % msg)


def warn(msg):
    sys.stderr.write("bump: WARNING: %s\n" % msg)


def natural_key(s):
    # Same digit-run rule as anyvm-org base-builder's gendata.natural_key
    # (the one ordering the whole ecosystem uses); builder/anyvm tags are
    # plain dotted numerics, but keep the general form so an exotic tag
    # cannot silently compare as a string.
    key = []
    for tok in re.split(r"[.\-_]", s):
        for part in re.findall(r"\d+|\D+", tok):
            if part.isdigit():
                key.append((0, int(part), ""))
            else:
                key.append((1, 0, part.lower()))
    return key


def strip_v(tag):
    return tag[1:] if tag.startswith("v") else tag


def _fetch(url, method="GET"):
    """GET returns the body; HEAD returns b"" on success. The image gate
    MUST use HEAD -- a *-vm image is >1 GB and there are dozens per repo,
    so a GET-based existence check would transfer tens of GB per nightly
    run and read each body whole into runner RAM."""
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": "vmactions-bump-bot/1.0"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(API):
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return b"" if method == "HEAD" else r.read()
    except urllib.error.HTTPError as e:
        raise FetchError("HTTP %d" % e.code)
    except Exception as e:
        raise FetchError(str(e))


def os_name():
    with open(DATAFILE, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\s*VM_OS_NAME=(\S+)\s*$", line)
            if m:
                return m.group(1)
    raise RuntimeError("VM_OS_NAME not found in %s" % DATAFILE)


def scan_confs():
    confs = []
    for fn in sorted(os.listdir(CONF_DIR)):
        if not fn.endswith(".conf") or fn == "default.release.conf":
            continue
        path = os.path.join(CONF_DIR, fn)
        with open(path, "rb") as f:
            data = f.read()
        vals = {}
        for line in data.splitlines(True):
            m = ASSIGN_RE.match(line)
            if m:
                vals[KEYMAP[m.group(1).decode()]] = m.group(2).decode().strip()
        confs.append({"name": fn, "path": path,
                      "tag": fn[:-len(".conf")],
                      "builder": vals.get("builder", ""),
                      "anyvm": vals.get("anyvm", "")})
    return confs


def lockstep_value(confs, key):
    vals = sorted(set(c[key] for c in confs if c[key]))
    if len(vals) != 1:
        raise MixedPins("%s is not lockstep across confs: %s"
                        % (key, ", ".join(vals) or "(none)"))
    return vals[0]


def rewrite_key(key, new, only=None):
    """Set KEY=new in the named confs (all confs when only is None).
    Byte-level, line-scoped, EOL-preserving: only the matched assignment
    line changes, every other byte survives exactly (a mixed-EOL fleet
    must not get whole-file diffs)."""
    keyb = key.encode()
    changed = 0
    for fn in sorted(os.listdir(CONF_DIR)):
        if not fn.endswith(".conf") or fn == "default.release.conf":
            continue
        if only is not None and fn not in only:
            continue
        path = os.path.join(CONF_DIR, fn)
        with open(path, "rb") as f:
            lines = f.readlines()
        hit = False
        out = []
        for line in lines:
            m = ASSIGN_RE.match(line)
            if m and m.group(1) == keyb and m.group(2).decode().strip() != new:
                line = keyb + b"=" + new.encode() + m.group(3)
                hit = True
            out.append(line)
        if hit:
            with open(path, "wb") as f:
                f.writelines(out)
            changed += 1
    return changed


def latest_release_tag(repo, fetch):
    data = fetch("%s/repos/%s/releases/latest" % (API, repo))
    return strip_v(json.loads(data.decode("utf-8"))["tag_name"])


def fetch_release_index(osname, builder, fetch):
    url = ("https://github.com/%s/%s-builder/releases/download/v%s/"
           "releases.json" % (BUILDER_ORG, osname, builder))
    data = fetch(url)
    doc = json.loads(data.decode("utf-8"))
    return doc["releases"]


def image_exists(osname, builder, tag, fetch):
    # Existence check for the image asset, so a tag cut before its build
    # finished is refused rather than pinned. HEAD, never GET: these are
    # 1+ GB files and this runs for every to-be-pinned tag.
    url = ("https://github.com/%s/%s-builder/releases/download/v%s/"
           "%s-%s.qcow2.zst" % (BUILDER_ORG, osname, builder, osname, tag))
    try:
        fetch(url, method="HEAD")
        return True
    except FetchError:
        return False


def _seed_conf(template_bytes, builder, entry):
    """Copy a template conf, then seed the fields releases.json knows.
    A field the template lacks is APPENDED, not silently dropped --
    openbsd's 7.2-7.6 era confs really do lack VM_SYNC_METHODS, and a new
    conf without it would fall back to legacy allow-all behavior."""
    text = template_bytes.decode("utf-8")
    text = re.sub(r"(?m)^BUILDER_VERSION=[^\r\n]*",
                  "BUILDER_VERSION=%s" % builder, text)
    for key, val in (("VM_SYNC_METHODS", entry["sync"]),
                     ("VM_SHUTDOWN_CMD", entry["shutdown"])):
        line = '%s="%s"' % (key, val)
        if re.search(r"(?m)^%s=" % key, text):
            text = re.sub(r"(?m)^%s=[^\r\n]*" % key, line, text)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += line + "\n"
    return text


def _variant_suffix(release, all_releases):
    """The '-<suffix>' making `release` a hyphen-extension of another
    release in the set, or "" when it is a base release itself."""
    for other in sorted(all_releases, key=len, reverse=True):
        if other != release and release.startswith(other + "-"):
            return release[len(other):]
    return ""


def plan_new_confs(index, builder):
    """Confs to create for releases the builder ships but this repo lacks.

    build:false rows are informational, not shipped images. desktop:true
    entries follow a PER-REPO policy inferred from the repo itself: they
    are created only when this repo already carries a conf for some
    desktop:true tag. The fleet is genuinely split -- freebsd-vm carries
    none of the builder's xfce/gnome/kde6 images, while ghostbsd-vm
    carries 26.1-xfce/-gershwin, which its builder marks desktop:true --
    so no global rule exists, and the repo's own conf set is the only
    authority on which side it is on.

    Template preference for each new conf: the newest existing conf with
    the SAME variant suffix and arch (r151060-build copies
    r151058-build), else the newest same-arch BASE conf. Sorting compares
    tags, never filenames -- '.conf' participating in a filename sort
    once ranked 26.1-xfce above 26.1.
    """
    entries = [e for e in index if e.get("build", True)]
    all_releases = set(e["release"] for e in entries)
    have_desktop = any(
        os.path.exists(os.path.join(CONF_DIR, "%s.conf" % e["tag"]))
        for e in entries if e.get("desktop"))
    by_arch = {}
    for fn in sorted(os.listdir(CONF_DIR)):
        if not fn.endswith(".conf") or fn == "default.release.conf":
            continue
        m = re.match(r"^(.*?)(-(aarch64|riscv64|powerpc64|sparc64|ppc64le"
                     r"|s390x|loongarch64|i386))?\.conf$", fn)
        arch = m.group(3) or "x86_64"
        by_arch.setdefault(arch, []).append(fn[:-len(".conf")])
    plan = []
    for entry in entries:
        if entry.get("desktop") and not have_desktop:
            continue
        suffix = "" if entry["arch"] == "x86_64" else "-" + entry["arch"]
        name = "%s%s.conf" % (entry["release"], suffix)
        if os.path.exists(os.path.join(CONF_DIR, name)):
            continue
        cands = by_arch.get(entry["arch"]) or by_arch.get("x86_64") or []
        if not cands:
            warn("no template conf for %s, skipping" % name)
            continue
        vsuf = _variant_suffix(entry["release"], all_releases)
        same_kind = [t for t in cands
                     if _variant_suffix(_strip_arch(t), all_releases) == vsuf]
        pool = same_kind or [t for t in cands
                             if not _variant_suffix(_strip_arch(t),
                                                    all_releases)]
        pool = pool or cands
        tpl = sorted(pool, key=natural_key)[-1] + ".conf"
        with open(os.path.join(CONF_DIR, tpl), "rb") as f:
            tpl_bytes = f.read()
        plan.append({"name": name, "release": entry["release"],
                     "content": _seed_conf(tpl_bytes, builder, entry),
                     "template": tpl})
    return plan


def _strip_arch(tag):
    m = re.match(r"^(.*?)-(aarch64|riscv64|powerpc64|sparc64|ppc64le"
                 r"|s390x|loongarch64|i386)$", tag)
    return m.group(1) if m else tag


def append_test_releases(releases):
    path = os.path.join(CONF_DIR, "test.releases")
    if not os.path.exists(path):
        warn("no %s, not appending" % path)
        return False
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    present = set(re.findall(r'"([^"]+)"', text))
    todo = [r for r in dict.fromkeys(releases) if r not in present]
    if not todo:
        return False
    line = text.rstrip("\n")
    line = line + ", " + ", ".join('"%s"' % r for r in todo)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(line + "\n")
    return True


def main(argv=None, fetch=_fetch):
    ap = argparse.ArgumentParser(
        description="propagate builder/anyvm releases into this *-vm repo")
    ap.add_argument("--check", action="store_true",
                    help="print the plan; write nothing")
    args = ap.parse_args(argv)
    if not os.path.isdir(CONF_DIR):
        sys.stderr.write("bump: no conf/ here; run from a *-vm repo root\n")
        return 1

    osname = os_name()
    confs = scan_confs()
    if not confs:
        sys.stderr.write("bump: no conf files found\n")
        return 1
    # ANYVM_VERSION is lockstep by design (verified 128/128 across the
    # fleet); refuse mixed values -- a bot must not "complete" a
    # half-finished hand edit. BUILDER_VERSION is deliberately NOT
    # checked: openbsd-vm legitimately carries four values (7.2-7.6
    # images exist only at old builder tags).
    try:
        cur_anyvm = lockstep_value(confs, "anyvm")
    except MixedPins as e:
        sys.stderr.write("bump: %s -- fix by hand first\n" % e)
        return 1

    rc = 0
    wrote = False

    # ---- builder version ----
    try:
        latest = latest_release_tag(
            "%s/%s-builder" % (BUILDER_ORG, osname), fetch)
    except FetchError as e:
        sys.stderr.write("bump: cannot read %s-builder latest release: %s\n"
                         % (osname, e))
        return 1
    newest_pin = sorted((c["builder"] for c in confs if c["builder"]),
                        key=natural_key)[-1]
    if natural_key(latest) <= natural_key(newest_pin):
        log("builder %s is current (latest %s)" % (newest_pin, latest))
    else:
        log("builder %s -> %s" % (newest_pin, latest))
        try:
            index = fetch_release_index(osname, latest, fetch)
        except FetchError as e:
            sys.stderr.write(
                "bump: v%s has no releases.json asset (%s); a post-gendata "
                "builder release must carry it -- not bumping\n"
                % (latest, e))
            return 1
        shipped_tags = set(e["tag"] for e in index if e.get("build", True))
        if not shipped_tags:
            sys.stderr.write("bump: v%s index lists no shipped releases\n"
                             % latest)
            return 1
        # Per-conf: bump only confs whose tag the new release actually
        # ships; a conf absent from the index keeps its old pin (its
        # image lives only there -- the openbsd legacy case).
        to_bump = [c for c in confs
                   if c["tag"] in shipped_tags
                   and natural_key(latest) > natural_key(c["builder"])]
        left = [c["name"] for c in confs if c["tag"] not in shipped_tags]
        if left:
            log("not shipped by v%s, keeping old pins: %s"
                % (latest, ", ".join(left)))
        plan = plan_new_confs(index, latest)
        # Image gate BEFORE any write: every conf about to be pinned to
        # v<latest> -- bumped or newly created -- must have its image
        # asset. A tag cut before its build finished is refused whole.
        missing = []
        for tag in ([c["tag"] for c in to_bump]
                    + [p["name"][:-len(".conf")] for p in plan]):
            if not image_exists(osname, latest, tag, fetch):
                missing.append(tag)
        if missing:
            sys.stderr.write(
                "bump: v%s is missing image asset(s) for %s -- tag cut "
                "before the build finished? not bumping\n"
                % (latest, ", ".join(missing)))
            return 1
        if args.check:
            log("would set BUILDER_VERSION=%s in %d conf(s)"
                % (latest, len(to_bump)))
            for p in plan:
                log("would create conf/%s (from %s)"
                    % (p["name"], p["template"]))
        else:
            n = rewrite_key("BUILDER_VERSION", latest,
                            only=[c["name"] for c in to_bump])
            log("BUILDER_VERSION=%s in %d conf(s)" % (latest, n))
            for p in plan:
                path = os.path.join(CONF_DIR, p["name"])
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(p["content"])
                log("created conf/%s (from %s)" % (p["name"], p["template"]))
            new_releases = [p["release"] for p in plan]
            if new_releases and append_test_releases(new_releases):
                log("test.releases += %s"
                    % ", ".join(dict.fromkeys(new_releases)))
            wrote = True

    # ---- anyvm version (lockstep follow, asset-gated) ----
    try:
        alatest = latest_release_tag("%s/anyvm" % BUILDER_ORG, fetch)
    except FetchError as e:
        warn("cannot read anyvm latest release: %s" % e)
        alatest = ""
    if alatest and natural_key(alatest) > natural_key(cur_anyvm):
        asset = ("https://github.com/%s/anyvm/releases/download/v%s/anyvm.py"
                 % (BUILDER_ORG, alatest))
        have_asset = True
        try:
            fetch(asset)
        except FetchError:
            have_asset = False
        if not have_asset:
            # not an error: the release exists but is not consumable yet
            # (index.js has no raw fallback); wait for the asset.
            warn("anyvm v%s has no anyvm.py asset yet, not following"
                 % alatest)
        elif args.check:
            log("would set ANYVM_VERSION=%s in every conf" % alatest)
        else:
            n = rewrite_key("ANYVM_VERSION", alatest)
            log("ANYVM_VERSION=%s in %d conf(s)" % (alatest, n))
            wrote = True
    elif alatest:
        log("anyvm %s is current (latest %s)" % (cur_anyvm, alatest))

    if wrote:
        log("done; commit is the workflow's job")
    return rc


if __name__ == "__main__":
    sys.exit(main())
