"""debsb - Debian kernel build from salsa.debian.org."""

import glob
import os
import shutil
import subprocess
from pathlib import Path

SALSA_URL = "https://salsa.debian.org/kernel-team/linux.git"
DEFAULT_BRANCH = "debian/latest"


def _host_arch():
    """Return dpkg architecture name."""
    return subprocess.check_output(["dpkg", "--print-architecture"]).decode().strip()


def debian_build(debsb_dir, branch, configitems, verbose=False, reset=False):
    """Clone Debian kernel from salsa, apply configitems, build amd64 .deb packages.

    Steps:
      1. git clone --depth 1 -b <branch> from salsa into ~/.debsb/linux
      2. debian/rules setup
      3. debian/rules source
      4. Apply --configitem entries to debian/config/config
      5. DEB_RULES_REQUIRES_ROOT=no make -f debian/rules.gen binary-arch_amd64_none_amd64

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

    # Step 2: Generate debian/control (intentionally fails with exit 1 even on success)
    print("=== Generating debian/control ===")
    subprocess.run(["make", "-f", "debian/rules", "debian/control"],
                   cwd=linux_dir, env=env)

    # Step 3: Download orig tarball
    print("=== Downloading orig tarball ===")
    changelog = os.path.join(linux_dir, "debian", "changelog")
    ver_line = Path(changelog).read_text().split("\n")[0]
    upstream_ver = ver_line.split("(")[1].split("-")[0]  # e.g. "7.1~rc6"
    orig_tar_pattern = os.path.join(os.path.dirname(linux_dir),
                                    f"linux_{upstream_ver}.orig.tar.*")
    if not glob.glob(orig_tar_pattern):
        korg_ver = upstream_ver.replace("~", "-")  # 7.1~rc6 -> 7.1-rc6
        orig_tar = os.path.join(os.path.dirname(linux_dir),
                                f"linux_{upstream_ver}.orig.tar.xz")
        # Download from kernel.org
        url = f"https://git.kernel.org/torvalds/t/linux-{korg_ver}.tar.gz"
        dl_tar = os.path.join(os.path.dirname(linux_dir), f"linux-{korg_ver}.tar.gz")
        ret = subprocess.run(["wget", "-q", "-O", dl_tar, url])
        if ret.returncode != 0:
            url = f"https://cdn.kernel.org/pub/linux/kernel/v{korg_ver.split('.')[0]}.x/linux-{korg_ver}.tar.xz"
            dl_tar = os.path.join(os.path.dirname(linux_dir), f"linux-{korg_ver}.tar.xz")
            subprocess.check_call(["wget", "-q", "-O", dl_tar, url])
        # Repack: rename top-level dir from linux-7.1-rc6 to linux-7.1~rc6
        import tempfile
        tmpdir = tempfile.mkdtemp(dir=os.path.dirname(linux_dir))
        if dl_tar.endswith(".gz"):
            subprocess.check_call(["tar", "-xzf", dl_tar, "-C", tmpdir])
        else:
            subprocess.check_call(["tar", "-xJf", dl_tar, "-C", tmpdir])
        extracted = os.path.join(tmpdir, f"linux-{korg_ver}")
        renamed = os.path.join(tmpdir, f"linux-{upstream_ver}")
        os.rename(extracted, renamed)
        subprocess.check_call(["tar", "-cJf", orig_tar,
                               "-C", tmpdir, f"linux-{upstream_ver}"])
        shutil.rmtree(tmpdir)
        os.remove(dl_tar)
        if not glob.glob(orig_tar_pattern):
            print(f"error: failed to create orig tarball", file=__import__('sys').stderr)
            __import__('sys').exit(1)

    # Step 4: debian/rules orig (extract upstream + apply quilt patches)
    print("=== debian/rules orig ===")
    subprocess.run(["make", "-f", "debian/rules", "orig"],
                   cwd=linux_dir, env=env)

    # Step 5: debian/rules source
    print("=== debian/rules source ===")
    subprocess.check_call(["make", "-f", "debian/rules.gen", "source"],
                          cwd=linux_dir, env=env)

    # Step 6: Apply --configitem to debian/config/config
    if configitems:
        config_path = os.path.join(linux_dir, "debian", "config", "config")
        print(f"=== Applying {len(configitems)} config items to debian/config/config ===")
        with open(config_path, "a") as f:
            for item in configitems:
                f.write(f"\n{item}")
                if verbose:
                    print(f"  + {item}")

    # Step 7: debian/rules setup
    print("=== debian/rules setup ===")
    subprocess.check_call(["make", "-f", "debian/rules.gen", f"setup_{_host_arch()}"],
                          cwd=linux_dir, env=env)

    # Step 8: Build binary package for host arch
    arch = _host_arch()
    print(f"=== Building {arch} kernel package (parallel={cpus}) ===")
    env["DEB_RULES_REQUIRES_ROOT"] = "no"
    build_start = os.path.join(linux_dir, ".debsb_build_marker")
    Path(build_start).touch()
    subprocess.check_call(
        ["make", "-f", "debian/rules.gen", f"binary-arch_{arch}_none_{arch}"],
        cwd=linux_dir, env=env)

    # Find the kernel .deb files newer than build start
    parent = os.path.dirname(linux_dir)
    arch = _host_arch()
    marker_time = os.path.getmtime(build_start)
    debs = [f for f in glob.glob(os.path.join(parent, f"linux-*{arch}*.deb"))
            if "dbg" not in f and os.path.getmtime(f) > marker_time]
    os.remove(build_start)
    if not debs:
        return None
    return debs
