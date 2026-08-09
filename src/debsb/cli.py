"""debsb - Debian Sid Sandbox using QEMU cloud images."""

import argparse
import glob
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DEBSB_DIR = os.path.join(str(Path.home()), ".debsb")

# Debian architecture name -> everything that differs between guests.  Only the
# host architecture is ever used: debsb builds kernels natively and boots with
# KVM, so cross-architecture emulation is out of scope.
ARCHES = {
    "amd64": {
        "qemu": "qemu-system-x86_64",
        "qemu_pkg": "qemu-system-x86",
        # i440fx default machine, SeaBIOS, IDE disks -- no extra flags needed.
        "machine": [],
        # The default qemu64/host model already works under both KVM and TCG.
        "cpu_kvm": None,
        "cpu_tcg": None,
        "console": "ttyS0",
        # -drive without if= lands on the IDE bus, so the cloud image is sda.
        "disk_opts": "",
        "seed_opts": "media=cdrom",
        "root_dev": "/dev/sda",
        "nic": "nic",
        "gfx": ["-vga", "virtio"],
    },
    "arm64": {
        "qemu": "qemu-system-aarch64",
        "qemu_pkg": "qemu-system-arm",
        # 'virt' has no default machine on aarch64 and no legacy buses: the
        # guest boots off UEFI (see uefi_args) with virtio disks.
        "machine": ["-machine", "virt"],
        # 'virt' defaults to the 32-bit cortex-a15, which KVM rejects outright;
        # a CPU model is mandatory here, unlike on amd64.
        "cpu_kvm": "host",
        "cpu_tcg": "max",
        "console": "ttyAMA0",
        "disk_opts": "if=virtio",
        "seed_opts": "if=virtio",
        "root_dev": "/dev/vda",
        "nic": "nic,model=virtio-net-pci",
        "gfx": ["-device", "virtio-gpu-pci"],
    },
}

# AAVMF (edk2) firmware, needed to boot the arm64 cloud image.  Debian/Ubuntu
# ship it in qemu-efi-aarch64, Fedora in edk2-aarch64.
AAVMF_CODE = [
    "/usr/share/AAVMF/AAVMF_CODE.fd",
    "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
    "/usr/share/edk2/aarch64/QEMU_EFI.fd",
]
AAVMF_VARS = [
    "/usr/share/AAVMF/AAVMF_VARS.fd",
    "/usr/share/qemu-efi-aarch64/QEMU_VARS.fd",
    "/usr/share/edk2/aarch64/vars-template-pflash.raw",
]


def _host_arch():
    """Map uname machine to the Debian architecture name."""
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())


ARCH = _host_arch()
SPEC = ARCHES.get(ARCH)

IMAGE_NAME = f"debian-sid-generic-{ARCH}-daily"
IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/sid/daily/latest/"
    f"{IMAGE_NAME}.qcow2"
)
SSH_PORT = 2222
SSH_KEY = os.path.join(DEBSB_DIR, "id_ed25519")

REQUIRED_CMDS = {
    "cloud-localds": "cloud-image-utils",
    "mkpasswd": "whois",
    "wget": "wget",
    "ssh-keygen": "openssh-client",
}


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def check_arch():
    if SPEC is None:
        die(f"unsupported architecture: {platform.machine()} "
            f"(supported: {', '.join(sorted(ARCHES))})")


def check_deps():
    check_arch()
    required = dict(REQUIRED_CMDS, **{SPEC["qemu"]: SPEC["qemu_pkg"]})
    missing = [pkg for cmd, pkg in required.items() if not shutil.which(cmd)]
    if missing:
        die(f"missing packages: {' '.join(missing)}\n"
            f"  Install with: sudo apt install {' '.join(missing)}")
    if ARCH == "arm64" and not _first_existing(AAVMF_CODE):
        die("no AAVMF/edk2 firmware found; arm64 guests boot via UEFI.\n"
            "  Install with: sudo apt install qemu-efi-aarch64")


def _first_existing(paths):
    return next((p for p in paths if os.path.isfile(p)), None)


def kvm_available():
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def uefi_args():
    """pflash pair for UEFI guests: read-only firmware + writable var store.

    The var store has to be a per-sandbox copy, otherwise GRUB cannot persist
    its boot entry and every boot falls back to the removable media path.
    """
    if ARCH != "arm64":
        return []
    code = _first_existing(AAVMF_CODE)
    if not code:
        die("no AAVMF/edk2 firmware found; install qemu-efi-aarch64")
    varstore = os.path.join(DEBSB_DIR, "AAVMF_VARS.fd")
    if not os.path.isfile(varstore):
        template = _first_existing(AAVMF_VARS)
        if template:
            shutil.copyfile(template, varstore)
        else:
            # Same geometry as the code image; edk2 formats it on first boot.
            with open(varstore, "wb") as f:
                f.truncate(os.path.getsize(code))
    return [
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={varstore}",
    ]


def qemu_cmd(qcow2, cloud_img, hostfwd):
    """Base QEMU invocation shared by the first-boot, install and run paths."""
    def drive(path, fmt, extra):
        return f"file={path},format={fmt}" + (f",{extra}" if extra else "")

    cmd = [SPEC["qemu"], "-m", "4096", "-smp", "4"] + SPEC["machine"]
    kvm = kvm_available()
    if kvm:
        cmd += ["-enable-kvm"]
    cpu = SPEC["cpu_kvm"] if kvm else SPEC["cpu_tcg"]
    if cpu:
        cmd += ["-cpu", cpu]
    cmd += uefi_args()
    cmd += [
        "-drive", drive(qcow2, "qcow2", SPEC["disk_opts"]),
        "-drive", drive(cloud_img, "raw", SPEC["seed_opts"]),
        "-net", SPEC["nic"],
        "-net", f"user,hostfwd=tcp::{SSH_PORT}-:22" if hostfwd else "user",
        "-virtfs", f"local,path={DEBSB_DIR},mount_tag=share,security_model=none",
    ]
    return cmd


def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def qcow2_path():
    return os.path.join(DEBSB_DIR, f"{IMAGE_NAME}.qcow2")


def cloud_img_path():
    return os.path.join(DEBSB_DIR, f"{IMAGE_NAME}.img")


def ssh_opts():
    return ["-i", SSH_KEY, "-p", str(SSH_PORT),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes"]


def ssh_attempts(n):
    """Boot budget in 2s polls; TCG guests need several times longer than KVM."""
    return n if kvm_available() else n * 3


def kill_vm(proc=None, pidfile=None):
    if proc:
        proc.kill()
        proc.wait()
    elif pidfile and os.path.isfile(pidfile):
        try:
            pid = int(Path(pidfile).read_text().strip())
            os.kill(pid, 9)
        except (ValueError, OSError):
            pass
        os.remove(pidfile)


def boot_vm_and_ssh(qcow2, cloud_img, remote_cmd, verbose=False, output_file=None):
    """Boot VM, wait for SSH, run command as root, shutdown."""
    if port_in_use(SSH_PORT):
        die(f"port {SSH_PORT} already in use. Kill the other VM first.")

    vm_cmd = qemu_cmd(qcow2, cloud_img, hostfwd=True) + [
        "-display", "none", "-vga", "none",
    ]
    pidfile = os.path.join(DEBSB_DIR, "qemu.pid")
    if verbose:
        vm_cmd += ["-serial", "mon:stdio"]
        proc = subprocess.Popen(vm_cmd)
    else:
        vm_cmd += ["-serial", "null", "-monitor", "none", "-daemonize",
                   "-pidfile", pidfile]
        subprocess.check_call(vm_cmd)
        proc = None

    try:
        print("Waiting for SSH...")
        for _ in range(ssh_attempts(180)):
            try:
                subprocess.check_call(
                    ["ssh"] + ssh_opts() + ["-o", "ConnectTimeout=3", "root@localhost", "true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except (subprocess.CalledProcessError, OSError):
                time.sleep(2)
        else:
            kill_vm(proc, pidfile)
            die("SSH timeout")
    except (KeyboardInterrupt, Exception):
        print("\nInterrupted. Killing VM...")
        kill_vm(proc, pidfile)
        sys.exit(1)

    # Run command
    ssh_cmd = ["ssh"] + ssh_opts() + ["root@localhost"]
    if output_file:
        output = subprocess.check_output(ssh_cmd + [remote_cmd])
        Path(output_file).write_bytes(output)
    else:
        subprocess.check_call(ssh_cmd + [remote_cmd])

    # Shutdown
    try:
        subprocess.check_call(ssh_cmd + ["poweroff"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        pass
    if proc:
        proc.wait()
    else:
        time.sleep(5)


def setup_image(args):
    """Download cloud image, cloud-init, first boot. Returns when image is ready."""
    os.makedirs(DEBSB_DIR, exist_ok=True)
    qcow2 = qcow2_path()

    if os.path.isfile(qcow2):
        if args.reset:
            remove = True
        else:
            print("Existing cloud image found.")
            try:
                answer = input("Remove and rebuild from scratch? [y/N]: ").strip().lower()
            except EOFError:
                answer = "n"
            remove = (answer == "y")

        if remove:
            os.remove(qcow2)
            # The UEFI var store outlives the disk it points at, so drop it too
            # and let edk2 re-enroll boot entries against the fresh image.
            for extra in (cloud_img_path(), os.path.join(DEBSB_DIR, "AAVMF_VARS.fd")):
                if os.path.isfile(extra):
                    os.remove(extra)
            print("Image removed. Rebuilding...")
        else:
            print("Keeping current image.")
            return

    if os.path.isfile(qcow2):
        return

    # Generate SSH key
    if not os.path.isfile(SSH_KEY):
        subprocess.check_call(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", SSH_KEY, "-q"])
        print(f"SSH key generated: {SSH_KEY}")

    # Download image
    print("Downloading Debian Sid cloud image...")
    subprocess.check_call(["wget", "-q", "--show-progress", "-O", qcow2, IMAGE_URL])

    # Resize
    size = args.size or "20G"
    if size[-1].isdigit():
        die("--size requires a unit suffix (e.g. 20G, 50G)")
    print(f"Resizing image to {size}...")
    subprocess.check_call(["qemu-img", "resize", qcow2, size])

    # Cloud-init
    ssh_pubkey = Path(SSH_KEY + ".pub").read_text().strip()
    passwd_hash = subprocess.check_output(
        ["mkpasswd", "-m", "sha-512", "debian"]
    ).decode().strip()

    console = SPEC["console"]
    root_dev = SPEC["root_dev"]
    meta_file = os.path.join(DEBSB_DIR, "meta-data")
    user_file = os.path.join(DEBSB_DIR, "user-data")
    Path(meta_file).write_text("")
    Path(user_file).write_text(
        "#cloud-config\n"
        "users:\n"
        "  - name: debian\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    lock_passwd: false\n"
        f"    passwd: \"{passwd_hash}\"\n"
        f"    ssh_authorized_keys:\n"
        f"      - {ssh_pubkey}\n"
        "  - name: root\n"
        "    lock_passwd: false\n"
        f"    passwd: \"{passwd_hash}\"\n"
        f"    ssh_authorized_keys:\n"
        f"      - {ssh_pubkey}\n"
        "ssh_pwauth: true\n"
        "disable_root: false\n"
        "chpasswd: { expire: False }\n"
        "runcmd:\n"
        "  - sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config\n"
        "  - systemctl restart ssh\n"
        f"  - growpart {root_dev} 1 || true\n"
        f"  - resize2fs {root_dev}1 || true\n"
        f"  - mkdir -p /etc/systemd/system/serial-getty@{console}.service.d\n"
        f"  - printf '[Service]\\nExecStart=\\nExecStart=-/sbin/agetty --autologin debian --noclear %%I 115200 linux\\n' > /etc/systemd/system/serial-getty@{console}.service.d/autologin.conf\n"
        "  - systemctl daemon-reload\n"
        f"  - systemctl restart serial-getty@{console}.service\n"
        "  - sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT=\"quiet loglevel=0\"/' /etc/default/grub\n"
        "  - update-grub\n"
        "  - mkdir -p /mnt/debsb\n"
        "  - echo 'share /mnt/debsb 9p trans=virtio,nofail 0 0' >> /etc/fstab\n"
        "  - mount /mnt/debsb\n"
        "  - ln -sf /mnt/debsb /root/.debsb\n"
        "  - ln -sf /mnt/debsb /home/debian/.debsb\n"
    )

    cloud_img = cloud_img_path()
    subprocess.check_call(["cloud-localds", cloud_img, user_file, meta_file])
    print("Cloud-init image created.")

    # First boot
    print("Running first boot (cloud-init)...")
    boot_vm_and_ssh(qcow2, cloud_img,
                    "timeout 300 cloud-init status --wait || true",
                    verbose=args.verbose)
    print(f"Build complete. Image: {qcow2}")


def cmd_build(args):
    check_deps()
    setup_image(args)

    if args.debian:
        from debsb.debian import debian_build
        debs = debian_build(DEBSB_DIR, args.branch, args.configitem,
                            verbose=args.verbose, reset=args.reset)
        if not debs:
            die("no kernel .deb found after Debian kernel build")
        # Only install essential packages: base, binary-unsigned, modules
        install_debs = [d for d in debs
                        if "dbg" not in d and "headers" not in d and "bpf-dev" not in d]
        # Move .deb files into ~/.debsb/ for 9p access
        deb_basenames = []
        for deb in install_debs:
            dest = os.path.join(DEBSB_DIR, os.path.basename(deb))
            if deb != dest:
                shutil.move(deb, dest)
            deb_basenames.append(os.path.basename(dest))
        # Install into VM
        dpkg_args = " ".join(f"/mnt/debsb/{b}" for b in deb_basenames)
        print("=== Installing Debian kernel into VM ===")
        install_script = (
            "set -e && "
            f"dpkg -i --force-depends {dpkg_args} && "
            # New Debian packaging: vmlinuz is at /usr/lib/modules/*/vmlinuz.unsigned
            # Copy to /boot/ for GRUB detection
            "KVER=$(ls /usr/lib/modules/ | sort -V | tail -1) && "
            "if [ -f /usr/lib/modules/$KVER/vmlinuz.unsigned ]; then "
            "  cp /usr/lib/modules/$KVER/vmlinuz.unsigned /boot/vmlinuz-$KVER; "
            "fi && "
            "update-initramfs -c -k $KVER 2>/dev/null || true && "
            "grub-set-default 0 && update-grub && "
            "echo INSTALL_OK"
        )
        boot_vm_and_ssh(qcow2_path(), cloud_img_path(), install_script,
                        verbose=args.verbose)
        print("=== Debian kernel build complete ===")
        print(f"  Packages: {', '.join(deb_basenames)}")
        print("  Run with: debsb run")
        return

    kernel_dir = args.kernel_dir
    if not kernel_dir:
        return

    kernel_dir = os.path.abspath(os.path.expanduser(kernel_dir))
    if not os.path.isfile(os.path.join(kernel_dir, "Makefile")):
        die(f"not a kernel source tree: {kernel_dir}")

    qcow2 = qcow2_path()
    cloud_img = cloud_img_path()
    config_file = os.path.join(kernel_dir, ".config")

    # Step 1: Generate .config if not present
    if not os.path.isfile(config_file):
        print("=== Generating default kernel config ===")
        subprocess.check_call(["make", "defconfig"], cwd=kernel_dir)
        # Enable 9p for shared filesystem.  CONFIG_NET_9P is =m in the arm64
        # defconfig and unset in x86_64's, so enable it too -- olddefconfig
        # would otherwise drop the =y transport that depends on it.
        for opt in ("CONFIG_NET_9P", "CONFIG_NET_9P_VIRTIO", "CONFIG_9P_FS"):
            subprocess.check_call(["./scripts/config", "--enable", opt], cwd=kernel_dir)

    # Step 2: Apply --configitem
    if args.configitem:
        for item in args.configitem:
            key, _, val = item.partition("=")
            if val:
                subprocess.check_call(
                    ["./scripts/config", "--set-val", key, val], cwd=kernel_dir)
            else:
                subprocess.check_call(
                    ["./scripts/config", "--enable", key], cwd=kernel_dir)

    # Step 3: make olddefconfig
    print("=== Running make olddefconfig ===")
    subprocess.check_call(["make", "olddefconfig"], cwd=kernel_dir)

    # Step 4: make bindeb-pkg
    cpus = str(os.cpu_count() or 4)
    print(f"=== Building kernel (make -j{cpus} bindeb-pkg) ===")
    env = os.environ.copy()
    env["MAKEFLAGS"] = f"-j{cpus}"
    subprocess.check_call(["make", f"-j{cpus}", "bindeb-pkg"], cwd=kernel_dir, env=env)

    # Find linux-image .deb (bindeb-pkg outputs to parent of kernel_dir)
    parent = os.path.dirname(kernel_dir)
    debs = [f for f in glob.glob(os.path.join(parent, "linux-image-*.deb"))
            if "dbg" not in f]
    if not debs:
        die("no linux-image .deb found after bindeb-pkg")
    deb_file = sorted(debs, key=os.path.getmtime)[-1]

    # Move .deb into ~/.debsb/ so it's accessible via 9p share
    deb_dest = os.path.join(DEBSB_DIR, os.path.basename(deb_file))
    shutil.move(deb_file, deb_dest)
    print(f"Using deb: {deb_dest}")

    # Step 5: Install .deb into VM via 9p, update GRUB
    deb_basename = os.path.basename(deb_dest)
    print("=== Installing kernel into VM ===")
    install_script = (
        "set -e && "
        "mountpoint -q /mnt/debsb || mount -t 9p -o trans=virtio share /mnt/debsb && "
        f"dpkg -i /mnt/debsb/{deb_basename} && "
        "update-initramfs -c -k $(ls /lib/modules/ | sort -V | tail -1) 2>/dev/null || true && "
        "grub-set-default 0 && update-grub && "
        "echo INSTALL_OK"
    )
    boot_vm_and_ssh(qcow2, cloud_img, install_script, verbose=args.verbose)

    print("=== Kernel build complete ===")
    print(f"  .deb: {deb_dest}")
    print("  Run with: debsb run")


def cmd_run(args):
    check_arch()
    qcow2 = qcow2_path()
    cloud_img = cloud_img_path()
    if not os.path.isfile(qcow2):
        die("no image found. Run 'debsb build' first.")

    need_ssh = args.ssh or getattr(args, 'exec')

    if need_ssh and port_in_use(SSH_PORT):
        die(f"port {SSH_PORT} already in use. Is another VM running?")

    cmd = qemu_cmd(qcow2, cloud_img, hostfwd=need_ssh)

    if args.sound:
        cmd += ["-device", "intel-hda", "-device", "hda-duplex"]
    if args.append:
        cmd += ["-append", " ".join(args.append)]
    for opt in args.qemu_opts:
        cmd += opt.split()

    if args.gui or args.graphics:
        cmd += ["-display", "gtk"] + SPEC["gfx"]
        os.execvp(cmd[0], cmd)
    elif getattr(args, 'exec') or args.ssh:
        cmd += ["-display", "none", "-vga", "none",
                "-serial", "null", "-monitor", "none", "-daemonize"]
        subprocess.check_call(cmd)
        user = "root" if args.root else "debian"
        print("VM started. Waiting for SSH...")
        for _ in range(ssh_attempts(90)):
            try:
                subprocess.check_call(
                    ["ssh"] + ssh_opts() + ["-o", "ConnectTimeout=3", f"{user}@localhost", "true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except (subprocess.CalledProcessError, OSError):
                time.sleep(2)
        else:
            die("SSH timeout")
        if getattr(args, 'exec'):
            ret = subprocess.call(["ssh"] + ssh_opts() + [f"{user}@localhost", getattr(args, 'exec')])
            subprocess.call(["ssh"] + ssh_opts() + [f"{user}@localhost", "poweroff"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.exit(ret)
        print(f"Connecting as {user}...")
        print(f"  Reconnect: ssh -i {SSH_KEY} -p {SSH_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {user}@localhost")
        os.execvp("ssh", ["ssh"] + ssh_opts() + [f"{user}@localhost"])
    else:
        cmd += ["-display", "none", "-vga", "none", "-serial", "mon:stdio"]
        os.execvp(cmd[0], cmd)


def main():
    parser = argparse.ArgumentParser(prog="debsb", description="Debian Sid Sandbox")
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="Build sandbox (optionally with kernel)")
    p_build.add_argument("kernel_dir", nargs="?", default=None,
                         help="Path to kernel source tree (triggers kernel build)")
    p_build.add_argument("--debian", action="store_true",
                         help="Build Debian kernel from salsa.debian.org")
    p_build.add_argument("--branch", metavar="BRANCH",
                         help="Salsa branch (default: debian/latest)")
    p_build.add_argument("--configitem", action="append", default=[],
                         help="Kernel config item (e.g. CONFIG_KASAN=y). Can be repeated.")
    p_build.add_argument("--size", metavar="SIZE", help="Disk size (default: 20G)")
    p_build.add_argument("--verbose", action="store_true", help="Show VM serial output")
    p_build.add_argument("--reset", action="store_true", help="Reset image without asking")

    p_run = sub.add_parser("run", help="Boot the sandbox VM")
    p_run.add_argument("--ssh", action="store_true", help="Boot headless, open SSH session")
    p_run.add_argument("--root", action="store_true", help="Login as root (default: debian user)")
    p_run.add_argument("--exec", metavar="CMD", help="Boot, run command via SSH, then shutdown")
    p_run.add_argument("--gui", action="store_true", help="Graphical QEMU window")
    p_run.add_argument("--verbose", action="store_true", help="Extra QEMU debug output")
    p_run.add_argument("--append", "-a", action="append", default=[], help="Additional kernel boot options")
    p_run.add_argument("--sound", action="store_true", help="Enable audio device")
    p_run.add_argument("--graphics", "-g", action="store_true", help="Show graphical output")
    p_run.add_argument("--qemu-opts", "-o", action="append", default=[], help="Additional QEMU arguments")

    args = parser.parse_args()
    if args.command == "build":
        cmd_build(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()
        sys.exit(1)


def entry_point():
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)


if __name__ == "__main__":
    entry_point()
