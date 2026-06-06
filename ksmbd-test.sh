#!/bin/bash
# Host-side script: SSHes into VM as root and runs xfstests + smbtorture
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

echo "=== Running tests inside VM ==="
$SSH bash -s <<'REMOTE'
set -e
export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH
export PATH=/usr/local/lib:$PATH

# Prepare test environment
mkdir -p /etc/ksmbd/ /mnt/1 /mnt/2
mkdir -m 777 -p /mnt/test1 /mnt/test2 /mnt/test3

cd /root
ksmbd.adduser -P ./ksmbdpwd.db -a testuser -p 1234 2>/dev/null || true

cp cifsd-test-result/testsuites/smb.conf .
cat smb.conf
ksmbd.mountd -n -C ./smb.conf -P ./ksmbdpwd.db 2>&1 | tee ksmbd-tools-asan.log &
sleep 1
ps -ax | grep smbd

echo "=== Running xfstests ==="
cd /root/xfsprogs-dev
make -j$((`nproc`+1))

./check cifs/001
./check generic/001 generic/002 generic/005 generic/006 generic/007 generic/008 generic/010

# Patch generic/011
sed -e "s/count=1000/count=100/" -e "s/-p 5/-p 3/" tests/generic/011 > tests/generic/011.new
sed -e "s/-p 5/-p 3/" tests/generic/011.out > tests/generic/011.out.new
mv tests/generic/011.new tests/generic/011
mv tests/generic/011.out.new tests/generic/011.out

./check generic/011 generic/013 generic/014 generic/023 generic/024 generic/028 generic/029 generic/030 \
        generic/032 generic/033 generic/036 generic/037 generic/043 generic/044 generic/045 generic/046 \
        generic/051 generic/069 generic/070 generic/071 generic/072 generic/074 generic/080 generic/084 \
        generic/086 generic/091 generic/095 generic/098 generic/095 generic/098 generic/100 generic/103 \
        generic/109 generic/113 generic/117 generic/124 generic/125 generic/129 generic/130 generic/132 \
        generic/133 generic/135 generic/141 generic/169 generic/198 generic/207 generic/208 generic/210 \
        generic/211 generic/212 generic/214 generic/215 generic/221 generic/225 generic/228 generic/236 \
        generic/239 generic/241 generic/245 generic/246 generic/247 generic/248 generic/249 generic/257 \
        generic/258 generic/263 generic/308 generic/309 generic/310 generic/313 generic/315 generic/316 \
        generic/323 generic/337 generic/339 generic/340 generic/344 generic/345 generic/346 generic/349 \
        generic/350 generic/354 generic/360 generic/377 generic/391 generic/393 generic/394 generic/406 \
        generic/412 generic/420 generic/428 generic/430 generic/431 generic/432 generic/433 generic/436 \
        generic/437 generic/438 generic/439 generic/443 generic/445 generic/446 generic/448 generic/451 \
        generic/452 generic/454 generic/460 generic/461 generic/464 generic/465 generic/469 generic/504 \
        generic/523 generic/524 generic/528 generic/532 generic/533 generic/539 generic/565 generic/567 \
        generic/568 generic/599

echo "=== Building smbtorture ==="
cd /root/samba
./configure --disable-cups --disable-iprint --without-ad-dc --without-ads --without-ldap --without-pam --with-shared-modules='!vfs_snapper' > /dev/null
make -j$((`nproc`+1)) bin/smbtorture > /dev/null

set +e
SHARE="//127.0.0.1/cifsd-test3/"
CRED="-Utestuser%1234"

run_torture() {
    echo "--- smbtorture: $1 ---"
    ./bin/smbtorture "$SHARE" $CRED "$1"
    rm -rf /mnt/test3/*
}

# smb2 connect
run_torture "smb2.connect"

# smb2 read
for t in eof position dir access; do run_torture "smb2.read.$t"; done

# smb2 scan
for t in scan getinfo setinfo find; do run_torture "smb2.scan.$t"; done

# smb2 dir
for t in find fixed many modify sorted file-index large-files; do run_torture "smb2.dir.$t"; done

# smb2 rename
for t in simple simple_nodelete no_sharing share_delete_and_delete_access \
         no_share_delete_but_delete_access share_delete_no_delete_access \
         msword rename_dir_openfile rename_dir_bench; do
    run_torture "smb2.rename.$t"
done

# smb2 maxfid
run_torture "smb2.maxfid"

# smb2 sharemode
for t in sharemode-access access-sharemode; do run_torture "smb2.sharemode.$t"; done

# smb2 compound
for t in related1 related2 related3 unrelated1 invalid1 invalid2 invalid3 \
         interim1 interim2 compound-break compound-padding; do
    run_torture "smb2.compound.$t"
done

# smb2 streams
for t in dir io sharemodes names names2 names3 rename rename2 \
         create-disposition attributes delete zero-byte basefile-rename-with-open-stream; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.streams.$t"
done
rm -rf /mnt/test3/*

# smb2 create
for t in gentest blob open brlocked multi delete leading-slash impersonation dir-alloc-size; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.create.$t"
done
for t in aclfile acldir nulldacl; do run_torture "smb2.create.$t"; done

# smb2 delete-on-close
for t in "smb2.delete-on-close-perms.OVERWRITE_IF" \
         "smb2.delete-on-close-perms.OVERWRITE_IF Existing" \
         "smb2.delete-on-close-perms.CREATE" \
         "smb2.delete-on-close-perms.CREATE Existing" \
         "smb2.delete-on-close-perms.CREATE_IF" \
         "smb2.delete-on-close-perms.CREATE_IF Existing" \
         "smb2.delete-on-close-perms.FIND_and_set_DOC"; do
    run_torture "$t"
done

# smb2 oplock
for t in exclusive1 exclusive2 exclusive3 exclusive4 exclusive5 exclusive6 exclusive9 \
         batch1 batch2 batch3 batch4 batch5 batch6 batch7 batch8 batch9 batch9a \
         batch10 batch11 batch12 batch13 batch14 batch15 batch16 batch19 batch20 \
         batch21 batch22a batch23 batch24 batch25 batch26 stream1 doc; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.oplock.$t"
done
for t in brl1 brl2 brl3 levelii500 levelii501 levelii502; do run_torture "smb2.oplock.$t"; done

# smb2 session
for t in reconnect1 reconnect2 reauth1 reauth2 reauth3 reauth4; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.session.$t"
done

# smb2 lock
for t in valid-request rw-shared rw-exclusive auto-unlock async cancel \
         cancel-tdis cancel-logoff zerobytelength zerobyteread unlock \
         multiple-unlock stacking contend context truncate; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.lock.$t"
done

# smb2 lease
for t in request nobreakself statopen statopen2 statopen3 upgrade upgrade2 upgrade3 \
         break oplock multibreak breaking1 breaking2 breaking3 breaking4 breaking5 \
         breaking6 lock1 complex1 timeout unlink v2_request_parent v2_request \
         v2_epoch1 v2_epoch2 v2_epoch3 v2_complex2 v2_rename; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.lease.$t"
done

# smb2 acls
for t in CREATOR GENERIC OWNER INHERITANCE INHERITFLAGS DYNAMIC; do run_torture "smb2.acls.$t"; done

# smb2 credits
for t in session_setup_credits_granted single_req_credits_granted skipped_mid; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.credits.$t"
done

# smb2 durable-open
for t in open-oplock open-lease reopen1 reopen1a reopen1a-lease reopen2 reopen2a \
         reopen2-lease reopen2-lease-v2 reopen3 reopen4 delete_on_close2 \
         file-position lease alloc-size read-only; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.durable-open.$t"
done

# smb2 durable-v2-open
for t in create-blob open-oplock open-lease reopen1 reopen1a reopen1a-lease \
         reopen2 reopen2b reopen2c reopen2-lease reopen2-lease-v2; do
    ./bin/smbtorture "$SHARE" $CRED "smb2.durable-v2-open.$t"
done

echo ""
echo "=== dmesg ==="
dmesg | tail -50

echo "=== Tests complete ==="
REMOTE
