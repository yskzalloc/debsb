"""debsb - Debian kernel build from salsa.debian.org."""

import glob
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SALSA_URL = "https://salsa.debian.org/kernel-team/linux.git"
DEFAULT_BRANCH = "debian/latest"


def _host_arch():
    """Return dpkg architecture name."""
    return subprocess.check_output(["dpkg", "--print-architecture"]).decode().strip()


def _tar_topdir(tar_path):
    """Return the top-level directory name inside a tarball."""
    with tarfile.open(tar_path) as tar:
        first = tar.next()
        if first is None:
            return None
        return first.name.lstrip("./").split("/")[0]


def _copyright_excluded(linux_dir):
    """Parse the Files-Excluded field from debian/copyright (DFSG exclusions)."""
    patterns = []
    in_field = False
    copyright_path = Path(linux_dir, "debian", "copyright")
    for line in copyright_path.read_text().splitlines():
        if in_field:
            if line.startswith((" ", "\t")):
                patterns.append(line.strip())
                continue
            break
        if line.startswith("Files-Excluded:"):
            in_field = True
            rest = line.split(":", 1)[1].strip()
            if rest:
                patterns.append(rest)
    return patterns


def _repack_orig(dl_tar, parent, upstream_ver, korg_ver, excluded):
    """Repack a kernel.org tarball into linux_<ver>.orig.tar.xz.

    Matches what uscan would produce: the top-level directory is renamed
    from linux-7.2-rc6 to linux-7.2~rc6 and the Files-Excluded paths are
    dropped.  debian/rules orig requires the renamed directory.
    """
    orig_tar = os.path.join(parent, f"linux_{upstream_ver}.orig.tar.xz")
    tmpdir = tempfile.mkdtemp(dir=parent)
    env = dict(os.environ, XZ_OPT="-T0")
    try:
        subprocess.check_call(["tar", "-xaf", dl_tar, "-C", tmpdir])
        renamed = os.path.join(tmpdir, f"linux-{upstream_ver}")
        os.rename(os.path.join(tmpdir, f"linux-{korg_ver}"), renamed)
        for pattern in excluded:
            for path in glob.glob(os.path.join(renamed, pattern)):
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
        subprocess.check_call(["tar", "-cJf", orig_tar,
                               "-C", tmpdir, f"linux-{upstream_ver}"], env=env)
    finally:
        shutil.rmtree(tmpdir)
    return orig_tar


def _ensure_orig_tarball(linux_dir, upstream_ver):
    """Return a valid linux_<ver>.orig tarball path, creating one if needed.

    An existing tarball is only trusted if its top-level directory matches
    linux-<ver> with the Debian ~rc naming; a raw kernel.org tarball saved
    under the orig name (top dir linux-X-rcN) is repacked instead of
    re-downloaded, anything else is discarded.
    """
    parent = os.path.dirname(linux_dir)
    korg_ver = upstream_ver.replace("~", "-")  # 7.1~rc6 -> 7.1-rc6
    good_topdir = f"linux-{upstream_ver}"
    excluded = _copyright_excluded(linux_dir)

    # A wrongly-named extraction in ../orig would shadow the fixed tarball
    stale = os.path.join(parent, "orig", f"linux-{korg_ver}")
    if korg_ver != upstream_ver and os.path.isdir(stale):
        shutil.rmtree(stale)

    for tar in glob.glob(os.path.join(parent, f"linux_{upstream_ver}.orig.tar.*")):
        topdir = _tar_topdir(tar)
        if topdir == good_topdir:
            return tar
        print(f"{tar}: top-level dir is {topdir!r}, expected {good_topdir!r}")
        if topdir == f"linux-{korg_ver}":
            print("Repacking as a proper orig tarball")
            orig_tar = _repack_orig(tar, parent, upstream_ver, korg_ver, excluded)
            if os.path.realpath(tar) != os.path.realpath(orig_tar):
                os.remove(tar)
            return orig_tar
        print("Discarding it")
        os.remove(tar)

    url = f"https://git.kernel.org/torvalds/t/linux-{korg_ver}.tar.gz"
    dl_tar = os.path.join(parent, f"linux-{korg_ver}.tar.gz")
    print(f"Downloading {url}")
    ret = subprocess.run(["wget", "-q", "-O", dl_tar, url])
    if ret.returncode != 0:
        os.remove(dl_tar)  # wget -O leaves an empty file behind on failure
        if "-rc" in korg_ver:
            # Release candidates only exist as git snapshots, no cdn fallback
            print(f"error: failed to download {url}", file=sys.stderr)
            sys.exit(1)
        url = (f"https://cdn.kernel.org/pub/linux/kernel/"
               f"v{korg_ver.split('.')[0]}.x/linux-{korg_ver}.tar.xz")
        dl_tar = os.path.join(parent, f"linux-{korg_ver}.tar.xz")
        print(f"Downloading {url}")
        subprocess.check_call(["wget", "-q", "-O", dl_tar, url])
    try:
        return _repack_orig(dl_tar, parent, upstream_ver, korg_ver, excluded)
    finally:
        os.remove(dl_tar)


def debian_build(debsb_dir, branch, configitems, verbose=False, reset=False):
    """Clone Debian kernel from salsa, apply configitems, build host-arch .deb packages.

    <arch> below is the host dpkg architecture (amd64 or arm64); both use the
    'none' featureset and a flavour named after the architecture.

    Steps:
      1. git clone --depth 1 -b <branch> from salsa into ~/.debsb/linux
      2. Write --configitem entries to debian/config.local/<arch>/config.<arch>
      3. Generate debian/control + rules.gen
      4. Ensure orig tarball, debian/rules orig (also regenerates rules.gen)
      5. debian/rules source + setup
      6. DEB_RULES_REQUIRES_ROOT=no make -f debian/rules.gen binary-arch_<arch>_none_<arch>

    Returns path to the linux-image .deb file.
    """
    branch = branch or DEFAULT_BRANCH
    linux_dir = os.path.join(debsb_dir, "linux")
    cpus = str(os.cpu_count() or 4)
    env = os.environ.copy()
    env["MAKEFLAGS"] = f"-j{cpus}"
    env["DEB_BUILD_OPTIONS"] = f"terse parallel={cpus}"

    # Step 1: Clone
    if os.path.isdir(linux_dir):
        if reset:
            shutil.rmtree(linux_dir)
        else:
            print(f"Debian linux already in {linux_dir}")
            try:
                answer = input("Remove and clone again? [y/N]: ").strip().lower()
            except EOFError:
                answer = "n"
            if answer == "y":
                shutil.rmtree(linux_dir)
            else:
                print("Using existing clone.")

    if os.path.isdir(linux_dir):
        subprocess.check_call(["git", "fetch", "--depth", "1", "origin", branch],
                              cwd=linux_dir, env=env)
        subprocess.check_call(["git", "checkout", "FETCH_HEAD"], cwd=linux_dir, env=env)
    else:
        print(f"=== Cloning Debian kernel (branch: {branch}) ===")
        subprocess.check_call([
            "git", "clone", "--depth", "1", "-b", branch, SALSA_URL, linux_dir
        ], env=env)

    # Step 2: Apply --configitem via the official debian/config.local overlay.
    # gencontrol.py resolves each kernel config level against debian/config
    # and then debian/config.local, and bakes the resulting file list into
    # rules.gen (see debian/README.source "Kernel config files").  The
    # flavour-level file config.local/<arch>/config.<arch> merges last, so it
    # overrides all stock config files — but it must exist before
    # debian/control and rules.gen are generated.
    arch = _host_arch()
    local_dir = os.path.join(linux_dir, "debian", "config.local")
    shutil.rmtree(local_dir, ignore_errors=True)  # drop overrides of prior runs
    # Undo the tracked-file appends made by debsb < 0.2.4 on reused clones
    subprocess.run(["git", "checkout", "--", "debian/config"], cwd=linux_dir)
    if configitems:
        flavour_conf = os.path.join(local_dir, arch, f"config.{arch}")
        os.makedirs(os.path.dirname(flavour_conf))
        print(f"=== Writing {len(configitems)} config items to "
              f"debian/config.local/{arch}/config.{arch} ===")
        with open(flavour_conf, "w") as f:
            for item in configitems:
                f.write(f"{item}\n")
                if verbose:
                    print(f"  + {item}")

    # Step 3: Generate debian/control (intentionally fails with exit 1 even on success)
    print("=== Generating debian/control ===")
    subprocess.run(["make", "-f", "debian/rules", "debian/control"],
                   cwd=linux_dir, env=env)

    # Step 4: Ensure a valid orig tarball exists (validated, uscan-style)
    print("=== Preparing orig tarball ===")
    changelog = os.path.join(linux_dir, "debian", "changelog")
    ver_line = Path(changelog).read_text().split("\n")[0]
    upstream_ver = ver_line.split("(")[1].split("-")[0]  # e.g. "7.1~rc6"
    _ensure_orig_tarball(linux_dir, upstream_ver)

    # debian/rules orig (extract upstream + apply quilt patches); its final
    # control-real regenerates rules.gen, picking up debian/config.local.
    # Must be fatal on error: a quilt conflict or rsync failure here leaves
    # the tree without upstream source, and source/setup then fail cryptically.
    print("=== debian/rules orig ===")
    subprocess.check_call(["make", "-f", "debian/rules", "orig"],
                          cwd=linux_dir, env=env)

    # Step 5: debian/rules source
    print("=== debian/rules source ===")
    subprocess.check_call(["make", "-f", "debian/rules.gen", "source"],
                          cwd=linux_dir, env=env)

    # debian/rules setup
    print("=== debian/rules setup ===")
    subprocess.check_call(["make", "-f", "debian/rules.gen", f"setup_{arch}"],
                          cwd=linux_dir, env=env)

    # Step 6: Build binary package for host arch
    print(f"=== Building {arch} kernel package (parallel={cpus}) ===")
    env["DEB_RULES_REQUIRES_ROOT"] = "no"
    build_start = os.path.join(linux_dir, ".debsb_build_marker")
    Path(build_start).touch()
    # Build only the sub-targets we actually need:
    #   _base    -> linux-base            (config/docs)
    #   _binary  -> linux-binary-unsigned (vmlinuz)
    #   _modules -> linux-modules         (the .ko tree)
    # The full aggregate also builds _image-di/_installer (debian-installer
    # udebs), which fail for a fuzzing kernel that has ext4/virtio built-in
    # (=y) rather than as modules, and which we never use.
    for sub in ("base", "binary", "modules"):
        subprocess.check_call(
            ["make", "-f", "debian/rules.gen",
             f"binary-arch_{arch}_none_{arch}_{sub}"],
            cwd=linux_dir, env=env)

    # Find the kernel .deb files newer than build start
    parent = os.path.dirname(linux_dir)
    marker_time = os.path.getmtime(build_start)
    debs = [f for f in glob.glob(os.path.join(parent, f"linux-*{arch}*.deb"))
            if "dbg" not in f and os.path.getmtime(f) > marker_time]
    os.remove(build_start)
    if not debs:
        return None
    return debs
