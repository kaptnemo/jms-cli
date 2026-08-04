# Verification under chroot

With chroot=/tmp, the SFTP path /data/x.bin maps to /tmp/data/x.bin on disk;
md5sum runs against that path directly. On mismatch, re-read the source per
chunk with dd.

```bash
md5sum /tmp/data/x.bin
dd if=/tmp/data/x.bin bs=1M skip=... count=... | md5sum
```
