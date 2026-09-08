"""debsb - Debian kernel build from salsa.debian.org."""

import glob
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SALSA_URL = "https://salsa.debian.org/kernel-team/linux.git"
DEFAULT_BRANCH = "debian/latest"


def _installed_gcc_version(cross_prefix=""):
    """Newest installed gcc major version for the given toolchain prefix.

    Looks for {cross_prefix}gcc-N on PATH (e.g. gcc-16, aarch64-linux-gnu-gcc-15)
    and returns the highest N, or None if only an unversioned gcc is present.
    """
    best = None
    seen = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        for name in os.listdir(d):
            prefix = f"{cross_prefix}gcc-"
            if name.startswith(prefix) and name[len(prefix):].isdigit():
                ver = int(name[len(prefix):])
                if best is None or ver > best:
                    best = ver
    return best


def _align_kernel_compiler(linux_dir, cross_prefix=""):
    """Point debian/config/defines.toml's c_compiler at an installed gcc.

    The Debian kernel pins an exact compiler (e.g. c_compiler = 'gcc-16') and
    bakes {DEB_HOST_GNU_TYPE}-gcc-16 into rules.gen.  On a runner that only has
    an older gcc, Kconfig fails with "C compiler ' <triplet>-gcc-16' not found".
    Rewrite the pin to the newest gcc actually installed (matching the target's
    cross prefix) so the build uses a compiler that exists; if the pinned one
    is present, or none can be resolved, leave the file untouched.
    """
    defines = Path(linux_dir, "debian", "config", "defines.toml")
    if not defines.is_file():
        return
    text = defines.read_text()
    m = re.search(r"^(\s*c_compiler\s*=\s*)'gcc-(\d+)'\s*$", text, re.MULTILINE)
    if not m:
        return
    pinned = int(m.group(2))
    # If the exact pinned gcc exists for this toolchain, nothing to do.
    if shutil.which(f"{cross_prefix}gcc-{pinned}"):
        return
    have = _installed_gcc_version(cross_prefix)
    if have == pinned:
        return
    if have is not None:
        replacement = f"gcc-{have}"
    elif shutil.which(f"{cross_prefix}gcc"):
        # No versioned compiler, but an unversioned {triplet}-gcc exists (common
        # for cross packages like gcc-aarch64-linux-gnu): pin to that.
        replacement = "gcc"
    else:
        return
    new_text = text[:m.start()] + f"{m.group(1)}'{replacement}'\n" + text[m.end():]
    defines.write_text(new_text)
    print(f"=== Kernel pins gcc-{pinned}, which is not installed; "
          f"using {replacement} instead ===")


def _disable_dtb_build(linux_dir, arch):
    """Turn off the Debian packaging's device-tree build for <arch>.

    The arm64 flavour sets enable_dtb = true, which makes the packaging run
    `make dtbs` and compile the entire arch/<arch>/boot/dts tree.  debsb boots
    the guest on QEMU's 'virt' machine, which supplies its own device tree, so
    those DTBs are never used -- and a single unbuildable board DTB in a fresh
    rc kernel (e.g. x1p64100-microsoft-denali) then fails the whole package.
    Flipping the flag to false skips the DTB build entirely: faster, and it
    removes a class of failures that has nothing to do with the guest kernel.
    """
    defines = Path(linux_dir, "debian", "config", arch, "defines.toml")
    if not defines.is_file():
        return
    text = defines.read_text()
    new_text, n = re.subn(r"^(\s*enable_dtb\s*=\s*)true\s*$",
                          r"\1false", text, flags=re.MULTILINE)
    if n:
        defines.write_text(new_text)
        print(f"=== Disabling {arch} DTB build "
              f"(unused: the QEMU virt machine supplies its own DT) ===")


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


def debian_build(debsb_dir, branch, configitems, verbose=False, reset=False,
                 arch=None, cross=False):
    """Clone Debian kernel from salsa, apply configitems, build .deb packages.

    <arch> is the target dpkg architecture (amd64 or arm64); it defaults to the
    host's.  Both use the 'none' featureset and a flavour named after the
    architecture.  When <cross> is true the target differs from the host and the
    build is cross-compiled: dpkg's DEB_HOST_ARCH/CROSS_COMPILE machinery is
    driven so the arm64 flavour is produced on an x86_64 runner (and vice
    versa).

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

    arch = arch or _host_arch()
    if cross:
        # Debian kernel packaging honours CROSS_COMPILE and cross-builds when
        # DEB_HOST_ARCH differs from DEB_BUILD_ARCH.  Setting DEB_HOST_ARCH is
        # what flips gencontrol.py and rules.gen into cross mode; CROSS_COMPILE
        # then points Kbuild at the target's gcc.  The build architecture stays
        # the host's so all build-time helpers still run natively.
        triplet = {"amd64": "x86_64-linux-gnu-",
                   "arm64": "aarch64-linux-gnu-"}[arch]
        env["DEB_HOST_ARCH"] = arch
        env["CROSS_COMPILE"] = triplet
        # The compat 32-bit vDSO of the arm64 flavour needs the armhf gcc; the
        # Debian control lists it as an arm64 build-dep, and cross builds still
        # invoke it under CROSS_COMPILE_COMPAT.
        if arch == "arm64":
            env["CROSS_COMPILE_COMPAT"] = "arm-linux-gnueabihf-"
        print(f"=== Cross-building {arch} on {_host_arch()} "
              f"(CROSS_COMPILE={triplet}) ===")

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
    local_dir = os.path.join(linux_dir, "debian", "config.local")
    shutil.rmtree(local_dir, ignore_errors=True)  # drop overrides of prior runs
    # Undo the tracked-file appends made by debsb < 0.2.4 on reused clones
    subprocess.run(["git", "checkout", "--", "debian/config"], cwd=linux_dir)
    # The arm64 guest's console is the virt machine's PL011 UART: pin it on so
    # the serial console (and ~/.debsb/serial.log) works.  The stock Debian
    # arm64 config already sets these =y; making them explicit guarantees a
    # visible boot even if a --configitem or a future config change touches it.
    if arch == "arm64":
        configitems = list(configitems) + [
            "CONFIG_SERIAL_AMBA_PL011=y",
            "CONFIG_SERIAL_AMBA_PL011_CONSOLE=y",
        ]
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

    # Align the pinned compiler with what the runner actually has.  The Debian
    # kernel hardcodes c_compiler = 'gcc-<N>' (baked into rules.gen as
    # {triplet}-gcc-<N>); if that exact version is not installed the Kconfig
    # step fails.  For a cross build the compiler carries the target triplet,
    # so match against that prefix.  Must run before debian/control so the
    # value propagates into rules.gen.
    gcc_prefix = {"amd64": "x86_64-linux-gnu-",
                  "arm64": "aarch64-linux-gnu-"}[arch] if cross else ""
    _align_kernel_compiler(linux_dir, gcc_prefix)

    # The guest boots on QEMU's virt machine, which provides its own device
    # tree, so the packaged DTBs are unused; skip building them (also dodges a
    # single unbuildable board DTB failing the whole arm64 package).  Must run
    # before debian/control so enable_dtb propagates into rules.gen.
    _disable_dtb_build(linux_dir, arch)

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
