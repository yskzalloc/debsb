"""debsb - Debian Sid Sandbox using QEMU cloud images."""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DEBSB_DIR = os.path.join(str(Path.home()), ".debsb")
IMAGE_NAME = "debian-sid-generic-amd64-daily"
IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/sid/daily/latest/"
    "debian-sid-generic-amd64-daily.qcow2"
)
SSH_PORT = 2222
SSH_KEY = os.path.join(DEBSB_DIR, "id_ed25519")


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


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


def cmd_build(args):
    os.makedirs(DEBSB_DIR, exist_ok=True)
    qcow2 = qcow2_path()

    # Check existing image
    if os.path.isfile(qcow2):
        if args.reset:
            sel = 0
        else:
            options = ["Reset (delete and rebuild from scratch)",
                       "Keep current image (do nothing)"]
            print("Existing image found:")
            sel = 0
            try:
                import termios, tty
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                tty.setraw(fd)
                while True:
                    sys.stdout.write(f"\r\033[K")
                    for i, opt in enumerate(options):
                        marker = ">" if i == sel else " "
                        sys.stdout.write(f"\r\033[K  {marker} {opt}\n")
                    sys.stdout.write(f"\033[{len(options)}A")
                    sys.stdout.flush()
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":
                        sys.stdin.read(1)
                        arrow = sys.stdin.read(1)
                        if arrow == "A":
                            sel = max(0, sel - 1)
                        elif arrow == "B":
                            sel = min(len(options) - 1, sel + 1)
                    elif ch in ("\r", "\n"):
                        break
                    elif ch == "\x03":
                        raise KeyboardInterrupt
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                sys.stdout.write("\n" * len(options) + "\r")
            except (ImportError, termios.error):
                print("  0) Reset  1) Keep")
                sel = 0 if input("Choose [0/1]: ").strip() == "0" else 1

        if sel == 0:
            os.remove(qcow2)
            cloud = cloud_img_path()
            if os.path.isfile(cloud):
                os.remove(cloud)
            print("Image removed. Rebuilding...")
        else:
            print("Keeping current image.")
            return
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
    print(f"Resizing image to {size}...")
    subprocess.check_call(["qemu-img", "resize", qcow2, size])

    # Cloud-init
    ssh_pubkey = Path(SSH_KEY + ".pub").read_text().strip()
    # Generate password hash
    passwd_hash = subprocess.check_output(
        ["mkpasswd", "-m", "sha-512", "debian"]
    ).decode().strip()

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
        "  - growpart /dev/sda 1 || true\n"
        "  - resize2fs /dev/sda1 || true\n"
        "  - mkdir -p /etc/systemd/system/serial-getty@ttyS0.service.d\n"
        "  - printf '[Service]\\nExecStart=\\nExecStart=-/sbin/agetty --autologin debian --noclear %%I 115200 linux\\n' > /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf\n"
        "  - systemctl daemon-reload\n"
        "  - systemctl restart serial-getty@ttyS0.service\n"
        "  - sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT=\"quiet loglevel=0\"/' /etc/default/grub\n"
        "  - update-grub\n"
        "  - mkdir -p /mnt/debsb\n"
        "  - echo 'share /mnt/debsb 9p trans=virtio,nofail 0 0' >> /etc/fstab\n"
        "  - mount /mnt/debsb\n"
        "  - ln -sf /mnt/debsb /root/.debsb\n"
        "  - ln -sf /mnt/debsb /home/debian/.debsb\n"
    )

    cloud_img = cloud_img_path()
    subprocess.check_call([
        "cloud-localds", cloud_img, user_file, meta_file
    ])
    print("Cloud-init image created.")

    # First boot to apply cloud-init
    print("Running first boot (cloud-init)...")
    if port_in_use(SSH_PORT):
        die(f"port {SSH_PORT} already in use. Kill the other VM first.")

    vm_cmd = [
        "qemu-system-x86_64", "-m", "4096", "-smp", "4", "-enable-kvm",
        "-drive", f"file={qcow2},format=qcow2",
        "-drive", f"file={cloud_img},format=raw,media=cdrom",
        "-net", "nic", "-net", f"user,hostfwd=tcp::{SSH_PORT}-:22",
        "-display", "none", "-vga", "none",
    ]
    pidfile = os.path.join(DEBSB_DIR, "qemu.pid")
    if args.verbose:
        vm_cmd += ["-serial", "mon:stdio"]
        proc = subprocess.Popen(vm_cmd)
    else:
        vm_cmd += ["-serial", "null", "-monitor", "none", "-daemonize",
                   "-pidfile", pidfile]
        subprocess.check_call(vm_cmd)
        proc = None

    # Wait for SSH
    ssh_opts = ["-i", SSH_KEY, "-p", str(SSH_PORT),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes"]

    def kill_vm():
        if proc:
            proc.kill()
            proc.wait()
        elif os.path.isfile(pidfile):
            try:
                pid = int(Path(pidfile).read_text().strip())
                os.kill(pid, 9)
            except (ValueError, OSError):
                pass
            os.remove(pidfile)

    try:
        print("Waiting for SSH...")
        for _ in range(120):
            try:
                subprocess.check_call(
                    ["ssh"] + ssh_opts + ["-o", "ConnectTimeout=3", "root@localhost", "true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except (subprocess.CalledProcessError, OSError):
                time.sleep(2)
        else:
            kill_vm()
            die("SSH timeout during first boot")
    except (KeyboardInterrupt, Exception):
        print("\nInterrupted. Killing VM...")
        kill_vm()
        sys.exit(1)

    print("Cloud-init done. Shutting down...")
    try:
        subprocess.check_call(
            ["ssh"] + ssh_opts + ["root@localhost", "poweroff"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        pass

    if proc:
        proc.wait()
    else:
        time.sleep(5)

    print(f"Build complete. Image: {qcow2}")
    print(f"  SSH: ssh -i {SSH_KEY} -p {SSH_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@localhost")


def cmd_run(args):
    qcow2 = qcow2_path()
    cloud_img = cloud_img_path()
    if not os.path.isfile(qcow2):
        die("no image found. Run 'debsb build' first.")

    if port_in_use(SSH_PORT):
        die(f"port {SSH_PORT} already in use. Is another VM running?")

    cmd = [
        "qemu-system-x86_64", "-m", "4096", "-smp", "4", "-enable-kvm",
        "-drive", f"file={qcow2},format=qcow2",
        "-drive", f"file={cloud_img},format=raw,media=cdrom",
        "-net", "nic", "-net", f"user,hostfwd=tcp::{SSH_PORT}-:22",
        "-virtfs", f"local,path={DEBSB_DIR},mount_tag=share,security_model=none",
    ]

    if args.sound:
        cmd += ["-device", "intel-hda", "-device", "hda-duplex"]
    if args.append:
        cmd += ["-append", " ".join(args.append)]
    for opt in args.qemu_opts:
        cmd += opt.split()

    if args.gui or args.graphics:
        cmd += ["-display", "gtk", "-vga", "virtio"]
        os.execvp(cmd[0], cmd)
    elif getattr(args, 'exec') or args.ssh:
        cmd += ["-display", "none", "-vga", "none",
                "-serial", "null", "-monitor", "none", "-daemonize"]
        subprocess.check_call(cmd)
        user = "root" if args.root else "debian"
        print("VM started. Waiting for SSH...")
        ssh_opts = ["-i", SSH_KEY, "-p", str(SSH_PORT),
                    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "BatchMode=yes"]
        for _ in range(90):
            try:
                subprocess.check_call(
                    ["ssh"] + ssh_opts + ["-o", "ConnectTimeout=3", f"{user}@localhost", "true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except (subprocess.CalledProcessError, OSError):
                time.sleep(2)
        else:
            die("SSH timeout")
        if getattr(args, 'exec'):
            ret = subprocess.call(["ssh"] + ssh_opts + [f"{user}@localhost", getattr(args, 'exec')])
            subprocess.call(["ssh"] + ssh_opts + [f"{user}@localhost", "poweroff"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.exit(ret)
        print(f"Connecting as {user}...")
        print(f"  Reconnect: ssh -i {SSH_KEY} -p {SSH_PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {user}@localhost")
        os.execvp("ssh", ["ssh"] + ssh_opts + [f"{user}@localhost"])
    else:
        # Serial console (default TTY)
        cmd += ["-display", "none", "-vga", "none", "-serial", "mon:stdio"]
        os.execvp(cmd[0], cmd)


def main():
    parser = argparse.ArgumentParser(prog="debsb", description="Debian Sid Sandbox")
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="Download and initialize Debian Sid cloud image")
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
    p_run.add_argument("--graphics", "-g", action="store_true", help="Show graphical output instead of console")
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
