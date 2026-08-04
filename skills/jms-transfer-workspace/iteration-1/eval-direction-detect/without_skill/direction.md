# `jms sftp` direction detection

Rule: an argument with `:` is remote. 1) upload (dst remote); 2) download
(src remote); 3) both remote → relay.

Equivalent library calls: 1) `sftp_transfer`; 2) `sftp_transfer`;
3) `relay_transfer`.

(Validation script not run; no recorded output.)
