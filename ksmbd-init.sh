#!/bin/bash
# Host-side script: SSHes into VM as root and initializes ksmbd test environment
set -e

SSH_KEY="$HOME/.ssh/id_ed25519_automation"
SSH_OPTS="-i $SSH_KEY -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"
SSH="ssh $SSH_OPTS root@localhost"

echo "=== Waiting for VM SSH to be ready ==="
until $SSH true 2>/dev/null; do
    sleep 2
    echo -n "."
done
echo " connected!"

echo "=== Running init inside VM ==="
$SSH bash -s <<'REMOTE'
set -e

apt update -y
apt upgrade -y
apt install -y \
    libelf-dev wget tar gzip python3 git clang libdw-dev libdwarf-dev \
    cifs-utils linux-headers-$(uname -r) \
    autoconf libtool pkg-config libnl-3-dev libnl-genl-3-dev libglib2.0-dev \
    xfslibs-dev uuid-dev libtool-bin xfsprogs libgdbm-dev gawk fio attr libattr1-dev libacl1-dev libaio-dev \
    liblmdb-dev libgnutls28-dev libgpgme-dev libjansson-dev libarchive-dev \
    gnutls-bin libparse-yapp-perl libjson-perl \
    libgettextpo-dev gettext libinih-dev liburcu-dev libdevmapper-dev libicu-dev

cd /root
[ -d smb3-kernel ] || git clone -b for-next --depth 1 https://github.com/smfrench/smb3-kernel.git
[ -d ksmbd-tools ] || git clone -b v1-sanitizer-on-master --depth 1 https://github.com/yskzalloc/ksmbd-tools.git
[ -d cifsd-test-result ] || git clone https://github.com/namjaejeon/cifsd-test-result
[ -d samba ] || git clone --depth 1 https://github.com/samba-team/samba.git
[ -d xfsprogs-dev ] || git clone --single-branch --depth 1 https://kernel.googlesource.com/pub/scm/fs/xfs/xfsprogs-dev.git

export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH
export PATH=/usr/local/lib:$PATH
grep -q 'LD_LIBRARY_PATH=/usr/local/lib' /root/.bashrc || {
    echo 'export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH' >> /root/.bashrc
    echo 'export PATH=/usr/local/lib:$PATH' >> /root/.bashrc
}

useradd fsgqa 2>/dev/null || true
useradd 123456-fsgqa 2>/dev/null || true

# Build ksmbd-tools
cd /root/ksmbd-tools
if [ ! -f /usr/local/sbin/ksmbd.mountd ]; then
    ./autogen.sh
    ./configure
    make -j$((`nproc`+1))
    make install
    ldconfig
fi

# Compile CIFS & KSMBD modules
cd /root/smb3-kernel
git pull || true
make localmodconfig > /dev/null

./scripts/config --module CONFIG_CIFS
./scripts/config --enable CONFIG_CIFS_STATS2
./scripts/config --enable CONFIG_CIFS_ALLOW_INSECURE_LEGACY
./scripts/config --enable CONFIG_CIFS_UPCALL
./scripts/config --enable CONFIG_CIFS_XATTR
./scripts/config --enable CONFIG_CIFS_POSIX
./scripts/config --enable CONFIG_CIFS_DEBUG
./scripts/config --enable CONFIG_CIFS_DEBUG2
./scripts/config --enable CONFIG_CIFS_DEBUG_DUMP_KEYS
./scripts/config --enable CONFIG_CIFS_DFS_UPCALL
./scripts/config --enable CONFIG_CIFS_SWN_UPCALL
./scripts/config --enable CONFIG_CIFS_SMB_DIRECT
./scripts/config --enable CONFIG_CIFS_FSCACHE
./scripts/config --enable CONFIG_CIFS_ROOT
./scripts/config --enable CONFIG_CIFS_COMPRESSION

./scripts/config --module CONFIG_SMB_SERVER
./scripts/config --enable CONFIG_SMB_SERVER_SMBDIRECT
./scripts/config --enable CONFIG_SMB_SERVER_CHECK_CAP_NET_ADMIN
./scripts/config --enable CONFIG_SMB_SERVER_KERBEROS5

make olddefconfig
make -j$((`nproc`+1)) -C /lib/modules/$(uname -r)/build M=$(pwd)/fs/smb/server modules
make -j$((`nproc`+1)) -C /lib/modules/$(uname -r)/build M=$(pwd)/fs/smb/client modules

modprobe cifs 2>/dev/null || true
modprobe ksmbd 2>/dev/null || true
rmmod ksmbd 2>/dev/null || true
rmmod cifs 2>/dev/null || true

insmod fs/smb/client/cifs.ko
insmod fs/smb/server/ksmbd.ko

echo "=== Init complete ==="
uname -r
cd /root/smb3-kernel && git --no-pager log -1 --oneline
lsmod | grep -E 'cifs|ksmbd'
REMOTE
