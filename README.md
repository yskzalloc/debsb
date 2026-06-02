# debsb

Debian Sid sandbox in one command. Downloads a Debian cloud image, boots it in QEMU/KVM, and gives you an isolated VM with auto-login, SSH, and shared filesystem. No kernel building, no complex setup.

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

### Run the sandbox

```bash
# Serial console (auto-login as debian, Ctrl-A X to quit)
debsb run

# SSH session (as debian)
debsb run --ssh

# SSH as root
debsb run --ssh --root

# Graphical QEMU window
debsb run --graphics

# With sound
debsb run --sound

# Extra QEMU options
debsb run --qemu-opts='-m 8192'
```

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

## Accounts

| User | Access |
|------|--------|
| `debian` | Serial auto-login, SSH with key, sudo NOPASSWD |
| `root` | SSH with key (`--root` flag) |

SSH key: `~/.debsb/id_ed25519` (auto-generated on first build)

## License

GPL-2.0-only
