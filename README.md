# debsb

Debian Sid sandbox in one command. Downloads a Debian cloud image, boots it in QEMU/KVM, and gives you an isolated VM with auto-login, SSH, and shared filesystem. No kernel building, no complex setup.

![debsb tutorial](img/debsb.gif)

## Motivation

I just want a fully hackable Debian setup—both user space and kernel—within a restricted sandbox.

I want to avoid rebuilding an OpenSSH-enabled rootfs for each fuzzing test and repeatedly using the `-kernel` and `-initrd` flags in QEMU.

## Install

```bash
pip install debsb
```

### Dependencies

- `qemu-system-x86_64` (with KVM support)
- `cloud-image-utils` (`cloud-localds`)
- `whois` (`mkpasswd`)
- `wget`
- `ssh`

On Debian/Ubuntu:
```bash
sudo apt install qemu-system-x86 cloud-image-utils whois wget openssh-client
```

For `--debian` kernel builds, additionally:
```bash
sudo apt install python3-dacite python3-debian python3-jinja2 debhelper quilt rsync devscripts dh-python
```

## Usage

### Build the sandbox (one-time)

```bash
debsb build --size 20G
```

This downloads the Debian Sid cloud image, configures SSH keys and auto-login, and runs first boot. All artifacts are stored in `~/.debsb/`.

Use `--reset` to skip the prompt and rebuild from scratch:
```bash
debsb build --size 20G --reset
```

### Build with a custom kernel (upstream)

```bash
debsb build ~/linux --configitem CONFIG_KASAN=y --configitem CONFIG_KCOV=y
```

This:
1. Sets up the cloud image (if not already done)
2. Generates a default kernel config (`make defconfig`) with VM-essential options
3. Applies `--configitem` entries
4. Runs `make olddefconfig` and `make bindeb-pkg`
5. Installs the resulting `.deb` into the VM via GRUB

### Build with [Debian kernel](https://salsa.debian.org/kernel-team/linux)

This kernel image is built based on the official Debian repository.
- https://salsa.debian.org/kernel-team/linux

```bash
# Default branch (debian/latest)
debsb build --debian --configitem CONFIG_KASAN=y --configitem CONFIG_KCOV=y

# Specific branch
debsb build --debian --branch debian/sid
```

This clones the Debian kernel from `salsa.debian.org/kernel-team/linux.git`, applies config items to `debian/config/config`, and builds the amd64 kernel package using the Debian packaging rules. The resulting kernel is installed into the VM via GRUB.

### Run the sandbox

```bash
# Serial console (auto-login as debian, Ctrl-A X to quit)
debsb run

# SSH session (as debian)
debsb run --ssh

# SSH as root
debsb run --ssh --root

# Run a command and auto-shutdown (requires --ssh)
debsb run --ssh --root --exec "apt update && apt upgrade -y"

# Graphical QEMU window
debsb run --graphics

# With sound
debsb run --sound

# Extra QEMU options
debsb run --qemu-opts='-m 8192'
```

### Execute a command

`--exec` runs a command via SSH and shuts down automatically. Requires `--ssh`:

```bash
debsb run --ssh --exec "uname -a"
debsb run --ssh --root --exec "ls -ahl .debsb"
debsb run --ssh --exec "./my-script.sh"
```

This is useful for CI/automation. The exit code of the command is propagated.

### Verbose mode

Show kernel boot messages:
```bash
debsb run --verbose
```

## Shared filesystem

Your host `~/.debsb/` directory is mounted inside the VM at:
- `/root/.debsb` (symlink to `/mnt/debsb`)
- `/home/debian/.debsb` (symlink to `/mnt/debsb`)

This is automatic — no manual mounting needed.

## How it works

1. Downloads `debian-sid-generic-amd64-daily.qcow2` from cloud.debian.org
2. Creates a cloud-init ISO with SSH keys, user config, and auto-login
3. Boots the VM with QEMU/KVM and waits for cloud-init to finish
4. On subsequent `debsb run`, boots the prepared image directly

When building with a kernel (`debsb build <path>` or `debsb build --debian`):
- The kernel `.deb` is installed into the VM
- GRUB is updated to boot the new kernel by default
- No `-kernel` or `-initrd` flags needed — GRUB handles boot

## Accounts

| User | Access |
|------|--------|
| `debian` | Serial auto-login, SSH with key, sudo NOPASSWD |
| `root` | SSH with key (`--root` flag) |

SSH key: `~/.debsb/id_ed25519` (auto-generated on first build)

## License

GPL-2.0-only
