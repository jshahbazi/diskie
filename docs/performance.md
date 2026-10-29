# Scanner performance

The scanner enumerates directories using `getattrlistbulk`, fetching names, file types, inode identifiers, link counts, and allocated sizes in batches. Filesystems that do not support that API use a descriptor-based POSIX fallback.

A fixed worker pool shares a bounded queue of 2,048 directories. When the queue fills, workers process additional directories inline. A thread-local stack reuses 64 KiB metadata buffers while preserving a parent's buffer during nested inline work. Counters publish once per directory, reducing contention between workers. A sharded inode set deduplicates hard-linked files across workers.

The retained tree contains only entries of at least 100 MiB, while aggregate sizes include smaller entries. Parent nodes remain alive until their own enumeration and all child jobs complete.

## Recorded measurements

These were local measurements on an active Apple silicon Mac, not controlled laboratory benchmarks.

| Workload | Version 1.1, four workers | Revised scanner, six workers |
| --- | ---: | ---: |
| Worktree folder, 1,457,204 files | 8.71–8.96 s | 5.91 s |
| Package cache, 1,147,053 files | 4.34–4.68 s | 3.15 s |
| Full data volume, approximately 13 million files | 63.36 s | 54.86 s |

File counts and allocated-byte totals matched exactly between the folder benchmark runs. The full-volume scans covered the same root and top-level folders, but file counts changed slightly while the system was active. Unreadable entries were 408 and 409 respectively. The full-volume result represents about 13% less elapsed time; the folder benchmarks improved by roughly 30%.

Six workers performed about as well as eight on the measured worktree. Diskie therefore defaults to at most six, with fewer on lower-core-count machines. Disk access permissions, filesystem cache state, background I/O, hard-link density, and directory shape affect scan time.
